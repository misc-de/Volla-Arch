# plinius-vdec, the plugin's side of the MDP (mdp-client.c) where
# tests/daemons-security/test_dsec_vdec.py does not reach: the socket the plugin uses without
# PLINIUS_VDEC_MDP_SOCKET (the system's, here a copy of mdp-proto.h names one in tmp), a daemon
# that went away before the request, and a buffer that is no open fd. The daemon is a small
# fake server in the test that records what arrives.

import array
import os
import socket
import struct
import subprocess
import threading

import pytest

from conftest import GCOV_DIR
from dhw_harness import PKG, build_harness, sandboxed_source

VDEC = PKG / "plinius-vdec"
MAGIC = 0x504D4431
SCALE = 5 if GCOV_DIR else 1

HARNESS = r"""
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <gio/gio.h>
#include "mdp.h"

/* SRC|- CODED W H DST PITCH [DELAY_MS]: one request ("-": an fd that is not open) */
int main(int argc, char **argv)
{
	g_autoptr(GError) error = NULL;
	unsigned coded = atoi(argv[2]), height = atoi(argv[4]);
	int src = argv[1][0] == '-' ? 1000 : open(argv[1], O_RDWR);
	int dst = open(argv[5], O_RDWR);
	pv_mdp_free(NULL);
	PvMdp *mdp = pv_mdp_new(&error);
	if (!mdp) {
		printf("error %s\n", error->message);
		return 0;
	}
	if (argc > 7)
		g_usleep(atoi(argv[7]) * 1000);
	if (pv_mdp_hyfbc_to_nv12(mdp, src, pv_mdp_hyfbc_src_size(coded, height), coded, atoi(argv[3]), height, dst,
				 atoi(argv[6]), &error))
		puts("ok");
	else
		printf("error %s\n", error->message);
	pv_mdp_free(mdp);
	return 0;
}
"""


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    d = tmp_path_factory.mktemp("mdp-client")
    default = d / "default.socket"
    # mdp-client.c includes "mdp-proto.h" from its own directory: the copy next to it
    sandboxed_source(VDEC / "mdp-proto.h", d, [('"/run/plinius-vdec-mdp.socket"', f'"{default}"')])
    sandboxed_source(VDEC / "mdp-client.c", d, [])
    exe = build_harness(d, "client", HARNESS, flags=[f"-I{VDEC}", str(d / "mdp-client.c")], pkgs=("gio-2.0",))
    return exe, default


class Server:
    """Accepts one connection; answers ok, or closes at once with close_at_once"""

    def __init__(self, path, close_at_once=False):
        if path.exists():
            path.unlink()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.sock.bind(str(path))
        self.sock.listen(1)
        self.requests = []
        self.thread = threading.Thread(target=self.serve, args=(close_at_once,))
        self.thread.start()

    def serve(self, close_at_once):
        conn, _ = self.sock.accept()
        with conn:
            if close_at_once:
                return
            conn.settimeout(10 * SCALE)
            data, anc, _, _ = conn.recvmsg(64, socket.CMSG_SPACE(8))
            fds = array.array("i")
            for level, kind, payload in anc:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    fds.frombytes(payload)
            names = [os.path.basename(os.readlink(f"/proc/self/fd/{fd}")) for fd in fds]
            for fd in fds:
                os.close(fd)
            self.requests.append((struct.unpack("<5I", data), names))
            conn.send(struct.pack("<i124s", 1, b""))

    def close(self):
        self.thread.join(timeout=10 * SCALE)
        self.sock.close()


def files(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"\0" * 4096)
    dst.write_bytes(b"\0" * 4096)
    return src, dst


def call(exe, *args, **env):
    e = {k: v for k, v in os.environ.items() if k != "PLINIUS_VDEC_MDP_SOCKET"}
    e.update(ASAN_OPTIONS="detect_leaks=0:exitcode=86", **{k: str(v) for k, v in env.items()})
    r = subprocess.run([str(exe), *map(str, args)], capture_output=True, text=True, env=e, timeout=30 * SCALE)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.parametrize("env", [{}, {"PLINIUS_VDEC_MDP_SOCKET": ""}])
def test_without_a_socket_named_the_systems_is_used(client, tmp_path, env):
    exe, default = client
    srv = Server(default)
    src, dst = files(tmp_path)
    try:
        assert call(exe, src, 32, 32, 16, dst, 32, **env) == "ok"
    finally:
        srv.close()
    # The magic, coded width, size and pitch, and the two buffers in that order
    assert srv.requests == [((MAGIC, 32, 32, 16, 32), ["src", "dst"])]


def test_a_daemon_gone_before_the_request(client, tmp_path):
    exe, _ = client
    path = tmp_path / "gone.socket"
    srv = Server(path, close_at_once=True)
    src, dst = files(tmp_path)
    try:
        out = call(exe, src, 32, 32, 16, dst, 32, 300, PLINIUS_VDEC_MDP_SOCKET=path)
    finally:
        srv.close()
    assert out == "error MDP service: Broken pipe"


def test_a_buffer_that_is_no_fd(client, tmp_path):
    exe, _ = client
    path = tmp_path / "s.socket"
    srv = Server(path, close_at_once=True)
    _, dst = files(tmp_path)
    try:
        out = call(exe, "-", 32, 32, 16, dst, 32, PLINIUS_VDEC_MDP_SOCKET=path)
    finally:
        srv.close()
    assert out == "error MDP service: Bad file descriptor"
