# Shared helpers of the tests in tests/daemons-security (C programs that decide about security
# or privacy): sanitizer flags, the harness directory and small fake servers.
#
# Everything a harness touches lives in tmp_path: sockets, state files, homes. The harnesses in
# c/ replace syslog, D-Bus, shadow and PAM by fakes (see the comment at the top of each).

import functools
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

from conftest import GCOV_DIR, PKG, compile_c

HERE = Path(__file__).resolve().parent
CDIR = HERE / "c"

# Sanitizer reports must fail the test, not scroll by; leak checking is off because the
# daemons keep their state until they exit (and LeakSanitizer does not run under ptrace).
SAN_ENV = {
    "ASAN_OPTIONS": "detect_leaks=0:abort_on_error=0:exitcode=86:halt_on_error=1",
    "UBSAN_OPTIONS": "halt_on_error=1:print_stacktrace=1:exitcode=87",
}


@functools.lru_cache(maxsize=None)
def _sanitizers_work():
    cc = os.environ.get("CC", "cc")
    if shutil.which(cc) is None:
        return False
    probe_dir = Path(os.environ.get("TMPDIR", "/tmp"))
    src = probe_dir / f"dsec-san-probe-{os.getpid()}.c"
    out = probe_dir / f"dsec-san-probe-{os.getpid()}"
    try:
        src.write_text("int main(void) { return 0; }\n")
        r = subprocess.run([cc, "-fsanitize=address,undefined", "-o", str(out), str(src)],
                           capture_output=True)
        return r.returncode == 0 and subprocess.run([str(out)], env=san_env()).returncode == 0
    finally:
        for f in (src, out):
            if f.exists():
                f.unlink()


def san_flags():
    """AddressSanitizer + UBSan where the host has them (else plain flags: still tested)."""
    if _sanitizers_work():
        return ["-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                "-fno-omit-frame-pointer"]
    return []


def san_env(**extra):
    env = dict(os.environ)
    env.update(SAN_ENV)
    env["LC_ALL"] = "C.UTF-8"
    env.update({k: str(v) for k, v in extra.items()})
    return env


def build(tmp_dir, name, sources, flags=(), pkgs=(), sanitize=True):
    """compile_c with the harness directory on the include path and sanitizers if asked."""
    out = Path(tmp_dir) / name
    all_flags = [f"-I{CDIR}", f"-I{PKG}", *(san_flags() if sanitize else []), *flags]
    return compile_c(out, sources, flags=all_flags, pkgs=pkgs)


def run_harness(binary, script, timeout=30, **env):
    """Feeds a line script to a command harness; returns its stdout lines. A sanitizer report
    or a crash fails the test with stderr attached."""
    # Counting every line (coverage builds, -O0) makes the harnesses several times slower
    r = subprocess.run([str(binary)], input=script, capture_output=True, text=True,
                       timeout=timeout * (5 if GCOV_DIR else 1), env=san_env(**env))
    assert r.returncode == 0, f"harness exit {r.returncode}\n{r.stderr[-4000:]}"
    return r.stdout.splitlines()


class FakeUnixServer:
    """A Unix stream socket in tmp_path that answers each connection with the next scripted
    line (None: close without an answer) and records what each client sent. For the clients of
    plinius-pinguard (PAM modules, plinius-pattern), without the real daemon."""

    def __init__(self, path, answers):
        self.path = str(path)
        self.answers = list(answers)
        self.requests = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(8)
        self.sock.settimeout(10)
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            if self._stop:
                conn.close()
                return
            with conn:
                conn.settimeout(5)
                data = b""
                try:
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                except OSError:
                    pass
                self.requests.append(data)
                answer = self.answers.pop(0) if self.answers else None
                if answer is not None:
                    try:
                        conn.sendall(answer.encode() + b"\n")
                    except OSError:
                        pass

    def close(self):
        # close() does not wake a thread blocked in accept(): a last connection does
        self._stop = True
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
                wake.connect(self.path)
        except OSError:
            pass
        self.thread.join(timeout=5)
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def refused_socket(path):
    """A socket file nobody listens on (connect gives ECONNREFUSED); the caller closes it."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    return s
