# plinius-pinguard as a process: its main loop (several connections read at once, slow and
# silent clients, the size limit, socket activation, the idle exit) through the harness's daemon
# mode (c/dsec_pinguard_harness.c: the daemon's own main(), with failing kernel calls on
# request), the package's own test-pinguard as its check() runs it, and plinius-pinguard-check.

import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from conftest import PKG
from dsec_helpers import CDIR, FakeUnixServer, build, san_env

PINGUARD = PKG / "plinius-pinguard"
PIN = "123456"
ME = __import__("pwd").getpwuid(os.getuid()).pw_name


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    return build(tmp_path_factory.mktemp("pinguard-daemon"), "pinguard-daemon-harness",
                 [CDIR / "dsec_pinguard_harness.c"], flags=["-lcrypt"])


def wait_for(path, timeout=10):
    end = time.monotonic() + timeout
    while not os.path.exists(path):
        assert time.monotonic() < end, f"{path} did not appear"
        time.sleep(0.02)


class Daemon:
    """The harness in daemon mode on a socket, state directory and marker in tmp_path."""

    def __init__(self, exe, tmp_path, enabled=True, args=None, **env):
        self.sock = tmp_path / "socket"
        self.state = tmp_path / "state"
        self.state.mkdir(exist_ok=True)
        self.marker = tmp_path / "enabled"
        if enabled:
            self.marker.touch()
        env.setdefault("DSEC_SELF_PIN", PIN)
        if args is None:
            args = ["--listen", str(self.sock), "--state", str(self.state),
                    "--enabled", str(self.marker), "--boot-id", "boot-a"]
        self.proc = subprocess.Popen([str(exe), *args], stderr=subprocess.PIPE, text=True,
                                     env=san_env(**env))
        self.log = None
        if "--listen" in args:
            wait_for(self.sock)

    def connect(self):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Blocking: with the backlog full a Unix connect waits (non-blocking: EAGAIN)
        c.connect(str(self.sock))
        c.settimeout(30)
        return c

    def ask(self, password, kind=None, user=ME):
        c = self.connect()
        data = user.encode() + b"\0" + password.encode() + b"\0"
        if kind:
            data += kind.encode() + b"\0"
        c.sendall(data)
        c.shutdown(socket.SHUT_WR)
        answer = read_all(c)
        c.close()
        return answer

    def stop(self):
        """SIGTERM; returns the exit code (0: the counters of a coverage build are written)."""
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        rc = self.proc.wait(timeout=30)
        self.log = self.proc.stderr.read()
        return rc

    def counter(self):
        f = self.state / str(os.getuid())
        return f.read_text() if f.exists() else None


def read_all(c):
    """Everything up to the end of the stream; "<reset>" after it if the daemon closed with our
    request still unread (Linux resets such a Unix connection)."""
    data = b""
    while True:
        try:
            chunk = c.recv(256)
        except ConnectionResetError:
            return data.decode() + "<reset>"
        if not chunk:
            return data.decode()
        data += chunk


@pytest.fixture
def daemon(harness, tmp_path):
    started = []

    def start(**kw):
        d = Daemon(harness, tmp_path, **kw)
        started.append(d)
        return d
    yield start
    for d in started:
        if d.proc.poll() is None:
            d.proc.kill()
            d.proc.wait()


# ---- Requests over the socket ----

def test_checks_and_counts_over_the_socket(daemon):
    d = daemon()
    answers = [d.ask("0") for _ in range(6)] + [d.ask(PIN)]
    assert d.stop() == 0
    assert answers == [f"FAIL {i} 0\n" for i in range(1, 6)] + ["FAIL 6 30\n", "WAIT 30\n"]
    assert d.counter().startswith("2 6 boot-a ")


def test_right_pin_over_the_socket(daemon):
    d = daemon()
    assert d.ask("0") == "FAIL 1 0\n"
    assert d.ask(PIN) == "OK\n"
    assert d.stop() == 0
    assert d.counter().split()[:2] == ["2", "0"]


def test_request_sent_in_pieces(daemon):
    # Read as it comes: the request is whole at the end of the stream, not at the first read
    d = daemon()
    c = d.connect()
    c.sendall(ME.encode())
    time.sleep(0.2)
    c.sendall(b"\0" + PIN.encode())
    time.sleep(0.2)
    c.sendall(b"\0")
    c.shutdown(socket.SHUT_WR)
    assert read_all(c) == "OK\n"
    c.close()
    assert d.stop() == 0


def test_third_nul_ends_the_request_without_end_of_stream(daemon):
    d = daemon()
    c = d.connect()
    c.sendall(ME.encode() + b"\0\0attempt\0")     # the writing side stays open
    assert read_all(c) == "COUNTED 1 0\n"
    c.close()
    assert d.stop() == 0


def test_request_at_the_size_limit_is_checked_at_once(daemon):
    # 1024 bytes without the end: taken as they are (here malformed), not waited on
    d = daemon()
    c = d.connect()
    c.sendall(b"x" * 1024)
    assert read_all(c) == "ERROR\n"
    c.close()
    assert d.ask("0") == "FAIL 1 0\n"
    assert d.stop() == 0


def test_slow_clients_are_checked_or_dropped_after_the_read_timeout(daemon):
    d = daemon()
    whole = d.connect()
    whole.sendall(ME.encode() + b"\0" + b"0\0")    # complete, but never shut down
    partial = d.connect()
    partial.sendall(ME.encode())                   # not a request yet
    start = time.monotonic()
    # Meanwhile others are answered at once
    assert d.ask("", "attempt") == "COUNTED 1 0\n"
    assert time.monotonic() - start < 3
    assert read_all(whole) == "FAIL 2 0\n"
    assert read_all(partial) == ""
    assert time.monotonic() - start >= 4.5          # READ_TIMEOUT is 5 s
    whole.close()
    partial.close()
    assert d.stop() == 0
    assert d.counter().split()[:2] == ["2", "2"]


def test_more_silent_connections_than_slots(daemon):
    d = daemon()
    silent = [d.connect() for _ in range(33)]
    time.sleep(0.5)
    start = time.monotonic()
    assert d.ask("0") == "FAIL 1 0\n"
    assert time.monotonic() - start < 3
    # The oldest made room: they see the end of the stream without an answer
    silent[0].settimeout(5)
    assert silent[0].recv(16) == b""
    for c in silent:
        c.close()
    assert d.stop() == 0
    assert "32 connections without a request, dropping the oldest" in d.log


def test_signal_does_not_stop_the_daemon(daemon):
    d = daemon()
    d.proc.send_signal(signal.SIGUSR1)
    time.sleep(0.2)
    assert d.proc.poll() is None
    assert d.ask("0") == "FAIL 1 0\n"
    assert d.stop() == 0


# ---- Kernel calls that fail ----

def test_failed_accept_is_retried(daemon):
    d = daemon(DSEC_FAULTS="accept=1")
    assert d.ask("0") == "FAIL 1 0\n"
    assert d.stop() == 0


def test_connection_without_credentials_is_refused(daemon):
    # No peer uid: nobody to decide for, nothing counted
    d = daemon(DSEC_FAULTS="peercred=1")
    # Refused before the request was read: the answer may be lost in the reset
    assert d.ask("0") in ("ERROR\n", "ERROR\n<reset>", "<reset>")
    assert d.ask("0") == "FAIL 1 0\n"
    assert d.stop() == 0


def test_interrupted_read_goes_on(daemon):
    d = daemon(DSEC_FAULTS="eintr=1")
    assert d.ask(PIN) == "OK\n"
    assert d.stop() == 0


def test_read_error_drops_the_connection_uncounted(daemon):
    d = daemon(DSEC_FAULTS="eio=1")
    # No answer at all (closed with the request unread: a reset)
    assert d.ask("0") in ("", "<reset>")
    assert d.ask("0") == "FAIL 1 0\n"
    assert d.stop() == 0


def test_failing_poll_ends_the_daemon(daemon):
    d = daemon(DSEC_FAULTS="poll=2")
    assert d.ask("0") == "FAIL 1 0\n"
    assert d.proc.wait(timeout=30) == 1


# ---- Start ----

def test_boot_id_from_the_kernel(daemon, tmp_path):
    bootid = tmp_path / "boot_id"
    bootid.write_text("6f0c-boot\n")
    d = daemon(args=["--listen", str(tmp_path / "socket"), "--state", str(tmp_path / "state"),
                     "--enabled", str(tmp_path / "enabled")], DSEC_BOOT_ID_FILE=bootid)
    assert d.ask("", "attempt") == "COUNTED 1 0\n"
    assert d.stop() == 0
    assert d.counter().startswith("2 1 6f0c-boot ")


def test_without_a_boot_id_the_counter_says_so(daemon, tmp_path):
    d = daemon(args=["--listen", str(tmp_path / "socket"), "--state", str(tmp_path / "state"),
                     "--enabled", str(tmp_path / "enabled")])
    assert d.ask("", "attempt") == "COUNTED 1 0\n"
    assert d.stop() == 0
    assert d.counter().startswith("2 1 - ")


@pytest.mark.parametrize("args", [["--bogus"], ["--state"], ["--listen"]])
def test_usage(harness, args):
    r = subprocess.run([str(harness), *args], capture_output=True, text=True, timeout=30,
                       env=san_env())
    assert r.returncode == 2
    assert "Usage: plinius-pinguard" in r.stderr


@pytest.mark.parametrize("how", ["long", "missing-dir", "no-socket"])
def test_cannot_listen(harness, tmp_path, how):
    path = {"long": tmp_path / ("x" * 120), "missing-dir": tmp_path / "none" / "s",
            "no-socket": tmp_path / "s"}[how]
    env = san_env(DSEC_FAULTS="socket=1") if how == "no-socket" else san_env()
    r = subprocess.run([str(harness), "--listen", str(path), "--state", str(tmp_path)],
                       capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 1
    assert "plinius-pinguard: cannot listen on" in r.stderr


def activated(harness, tmp_path, listen_pid="$$", listen_fds="1", idle_ms=300):
    """Started as systemd does: the socket as fd 3, LISTEN_PID/LISTEN_FDS for it."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(tmp_path / "socket"))
    srv.listen(8)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    script = (f'LISTEN_PID={listen_pid} LISTEN_FDS={listen_fds} exec "$0" --state "$1" '
              f'--enabled "$2" --boot-id boot-a 3<&"$3"')
    proc = subprocess.Popen(["sh", "-c", script, str(harness), str(state),
                             str(tmp_path / "enabled"), str(srv.fileno())],
                            pass_fds=[srv.fileno()], stderr=subprocess.PIPE, text=True,
                            env=san_env(DSEC_IDLE_MS=idle_ms, DSEC_SELF_PIN=PIN))
    return srv, proc


def test_socket_activation_and_idle_exit(harness, tmp_path):
    (tmp_path / "enabled").touch()
    srv, proc = activated(harness, tmp_path)
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(30)
        c.connect(str(tmp_path / "socket"))
        c.sendall(ME.encode() + b"\0" + PIN.encode() + b"\0")
        c.shutdown(socket.SHUT_WR)
        assert read_all(c) == "OK\n"
        c.close()
        # Nothing more to do: the daemon exits by itself, the socket stays with systemd
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        srv.close()


@pytest.mark.parametrize("pid,fds", [("1", "1"), ("$$", "2"), ("$$", "0")])
def test_socket_activation_must_be_for_this_process(harness, tmp_path, pid, fds):
    srv, proc = activated(harness, tmp_path, listen_pid=pid, listen_fds=fds)
    try:
        assert proc.wait(timeout=30) == 1
        assert "start through plinius-pinguard.socket or with --listen" in proc.stderr.read()
    finally:
        srv.close()


def test_without_socket_activation_or_listen(harness, tmp_path):
    env = san_env()
    env.pop("LISTEN_PID", None)
    env.pop("LISTEN_FDS", None)
    r = subprocess.run([str(harness), "--state", str(tmp_path)], capture_output=True, text=True,
                       timeout=30, env=env)
    assert r.returncode == 1
    assert "start through plinius-pinguard.socket" in r.stderr


# ---- The package's own check() ----

@pytest.fixture(scope="module")
def selftest(tmp_path_factory):
    d = tmp_path_factory.mktemp("pinguard-check")
    # Built as the PKGBUILD builds them; a fork child's _exit still writes coverage counters
    daemon = build(d, "pinguard-pkg-daemon", [PINGUARD / "plinius-pinguard.c"],
                   flags=["-include", str(CDIR / "dsec_nolog.h"), "-lcrypt"], sanitize=False)
    test = build(d, "pinguard-pkg-selftest", [PINGUARD / "test-pinguard.c"],
                 flags=["-include", str(CDIR / "dsec_gcov_exit.h")], sanitize=False)
    return daemon, test


def test_package_check_passes(selftest, tmp_path):
    daemon, test = selftest
    work = tmp_path / "test"
    work.mkdir()
    r = subprocess.run([str(test), str(daemon), str(work)], capture_output=True, text=True,
                       timeout=180, env=san_env())
    assert r.returncode == 0, r.stderr
    assert r.stdout == "test-pinguard: all checks passed\n"
    # Nothing outside its directory
    assert {p.name for p in work.iterdir()} <= {"socket", "state", "enabled"}


def test_package_check_fails_against_a_broken_daemon(selftest, tmp_path):
    # A "daemon" that leaves a socket nobody listens on: every check must fail, not pass
    _, test = selftest
    broken = tmp_path / "broken-daemon"
    broken.write_text(f"#!{sys.executable}\nimport socket, sys\n"
                      "socket.socket(socket.AF_UNIX).bind(sys.argv[2])\n")
    broken.chmod(0o755)
    work = tmp_path / "test"
    work.mkdir()
    r = subprocess.run([str(test), str(broken), str(work)], capture_output=True, text=True,
                       timeout=180, env=san_env())
    assert r.returncode == 1
    assert "check(s) failed" in r.stderr
    # The answer buffer of a failed pinguard_ask() is printed empty, not as what was on the stack
    if os.getuid() != 0:
        assert 'root\'s password for anyone: ""' in r.stderr
    assert "FAIL " in r.stderr and "connect" in r.stderr
    assert "all checks passed" not in r.stdout


def test_package_check_usage(selftest):
    _, test = selftest
    r = subprocess.run([str(test)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "Usage: test-pinguard DAEMON DIR" in r.stderr


# ---- plinius-pinguard-check ----

@pytest.fixture(scope="module")
def check_tool(tmp_path_factory):
    return build(tmp_path_factory.mktemp("pinguard-check-tool"), "pinguard-check-tool",
                 [CDIR / "dsec_pinguard_check_harness.c"])


def run_check(exe, sock, stdin=b"123456", args=(), **env):
    return subprocess.run([str(exe), *args], input=stdin, capture_output=True, timeout=60,
                          env=san_env(DSEC_SOCKET=sock, **env))


@pytest.mark.parametrize("answer,status", [
    ("OK", 0), ("FAIL 1 0", 1), ("FAIL 6 30", 1), ("WAIT 30", 2),
    ("DENIED", 3), ("ERROR", 3), ("", 3),
])
def test_check_tool_exit_status(check_tool, tmp_path, answer, status):
    sock = tmp_path / "s"
    with FakeUnixServer(sock, [answer]) as srv:
        r = run_check(check_tool, sock)
    assert r.returncode == status
    if answer:
        assert r.stdout.decode() == answer + "\n"
    # The caller's own name, a plain check (no kind)
    assert srv.requests == [ME.encode() + b"\x00123456\x00"]


def test_check_tool_reads_up_to_the_first_nul(check_tool, tmp_path):
    sock = tmp_path / "s"
    with FakeUnixServer(sock, ["OK"]) as srv:
        assert run_check(check_tool, sock, stdin=b"1234\0rest").returncode == 0
    assert srv.requests == [ME.encode() + b"\x001234\0"]


def test_check_tool_cuts_an_overlong_password(check_tool, tmp_path):
    # At most REQUEST_MAX - 1 bytes are read; with the user name that is too long to send
    sock = tmp_path / "s"
    with FakeUnixServer(sock, ["OK"]) as srv:
        r = run_check(check_tool, sock, stdin=b"9" * 5000)
    assert r.returncode == 3
    assert srv.requests == []


def test_check_tool_without_a_daemon(check_tool, tmp_path):
    r = run_check(check_tool, tmp_path / "none")
    assert (r.returncode, r.stdout) == (3, b"")


def test_check_tool_usage(check_tool, tmp_path):
    r = run_check(check_tool, tmp_path / "none", args=["extra"])
    assert r.returncode == 3
    assert b"Usage: plinius-pinguard-check" in r.stderr


def test_check_tool_without_a_user_entry(check_tool, tmp_path):
    sock = tmp_path / "s"
    with FakeUnixServer(sock, ["OK"]) as srv:
        assert run_check(check_tool, sock, DSEC_NO_USER="1").returncode == 3
    assert srv.requests == []
