# plinius-pinguard: the counter of wrong PINs and the waits after them, through the daemon's own
# handle_request() with a fake boot clock, fake shadow entries and fake users
# (c/dsec_pinguard_harness.c), so the whole schedule up to an hour is checked without waiting.
# Plus the client helpers of plinius-pinguard-client.h and one run of the real daemon process.

import os
import pwd
import socket
import stat
import subprocess
import time

import pytest

from conftest import PKG
from dsec_helpers import CDIR, FakeUnixServer, build, run_harness, san_env

PINGUARD = PKG / "plinius-pinguard"
ALICE, BOB, ROOT = 1001, 1002, 0


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    return build(tmp_path_factory.mktemp("pinguard"), "pinguard-harness",
                 [CDIR / "dsec_pinguard_harness.c"], flags=["-lcrypt"])


def request(user, password, kind=None):
    data = user.encode() + b"\0" + password.encode() + b"\0"
    if kind is not None:
        data += kind.encode() + b"\0"
    return data


class Guard:
    """Collects commands for one harness run; run() returns the answers of the req lines."""

    def __init__(self, exe, tmp_path, enabled=True, boot="boot-a", clock=1000):
        self.exe = exe
        self.state = tmp_path / "state"
        self.state.mkdir(exist_ok=True)
        self.marker = tmp_path / "enabled"
        if enabled:
            self.marker.touch()
        elif self.marker.exists():
            self.marker.unlink()
        self.cmds = [f"dirs {self.state} {self.marker}", f"bootid {boot}", f"clock {clock}",
                     "hash alice 123456", "hash bob 654321"]

    def ask(self, password, kind=None, uid=ALICE, user="alice"):
        self.cmds.append(f"req {uid} {request(user, password, kind).hex()}")
        return self

    def raw(self, data, uid=ALICE):
        self.cmds.append(f"req {uid} {data.hex()}")
        return self

    def clock(self, seconds):
        self.cmds.append(f"clock {seconds}")
        return self

    def cmd(self, line):
        self.cmds.append(line)
        return self

    def run(self):
        out = run_harness(self.exe, "\n".join(self.cmds) + "\n")
        assert len(out) == len(self.cmds), out
        return [o for c, o in zip(self.cmds, out) if c.startswith("req ")]

    def state_file(self, uid=ALICE, network=False):
        return self.state / (f"{uid}.network" if network else f"{uid}")


# ---- The schedule (plinius-pinguard.h) ----

@pytest.mark.parametrize("failures,wait", [
    (0, 0), (1, 0), (5, 0), (6, 30), (7, 60), (8, 300), (9, 900), (10, 3600), (11, 3600),
    (1000, 3600), (4294967295, 3600),
])
def test_wait_after(harness, failures, wait):
    assert run_harness(harness, f"wait {failures}\n") == [str(wait)]


@pytest.mark.parametrize("seconds,text", [
    (0, "0 seconds"), (1, "1 second"), (59, "59 seconds"), (60, "1 minute"),
    (61, "2 minutes"), (3599, "60 minutes"), (3600, "1 hour"), (3601, "2 hours"),
])
def test_format_wait_rounds_up(harness, seconds, text):
    # Rounded up, so nobody is told to try before the wait is over
    assert run_harness(harness, f"format {seconds}\n") == [text]


@pytest.mark.parametrize("err,fallback", [
    (2, True),     # ENOENT: no socket
    (111, True),   # ECONNREFUSED: nobody listens
    (88, True),    # ENOTSOCK
    (110, False),  # ETIMEDOUT: a busy daemon must never open the uncounted way
    (22, False),   # EINVAL
    (13, False),   # EACCES
    (104, False),  # ECONNRESET
])
def test_only_a_missing_daemon_may_fall_back(harness, err, fallback):
    assert run_harness(harness, f"unreachable {err}\n") == ["yes" if fallback else "no"]


# ---- Counting ----

def test_full_backoff_schedule_on_the_boot_clock(harness, tmp_path):
    g = Guard(harness, tmp_path)
    for _ in range(5):
        g.ask("0000")
    g.ask("0000")                       # 6th: 30 s
    g.ask("0000")                       # right away: not checked
    g.clock(1029).ask("0000")           # 1 s left
    g.clock(1030).ask("0000")           # over: 7th, 60 s
    g.clock(1090).ask("0000")           # 8th, 5 min
    g.clock(1390).ask("0000")           # 9th, 15 min
    g.clock(2290).ask("0000")           # 10th, 1 h
    g.clock(5890).ask("0000")           # 11th, still 1 h
    assert g.run() == ["FAIL 1 0", "FAIL 2 0", "FAIL 3 0", "FAIL 4 0", "FAIL 5 0",
                       "FAIL 6 30", "WAIT 30", "WAIT 1", "FAIL 7 60", "FAIL 8 300",
                       "FAIL 9 900", "FAIL 10 3600", "FAIL 11 3600"]


def test_right_pin_is_not_even_checked_during_a_wait(harness, tmp_path):
    g = Guard(harness, tmp_path)
    for _ in range(6):
        g.ask("0000")
    g.ask("123456")                     # the right PIN, but a wait runs
    g.clock(1030).ask("123456")         # after it: accepted
    g.ask("0000")                       # and the count started over
    out = g.run()
    assert out[5:] == ["FAIL 6 30", "WAIT 30", "OK", "FAIL 1 0"]


def test_right_pin_clears_the_count(harness, tmp_path):
    g = Guard(harness, tmp_path)
    g.ask("1").ask("2").ask("3").ask("123456").ask("4")
    assert g.run() == ["FAIL 1 0", "FAIL 2 0", "FAIL 3 0", "OK", "FAIL 1 0"]
    assert g.state_file().read_text().split()[:2] == ["2", "1"]


def test_counter_is_written_with_boot_and_deadline(harness, tmp_path):
    g = Guard(harness, tmp_path)
    for _ in range(6):
        g.ask("0")
    g.run()
    # "2 <failures> <boot id> <next try on the boot clock>"
    assert g.state_file().read_text() == "2 6 boot-a 1030\n"
    assert not list(g.state.glob("*.tmp"))


def test_wait_off_counts_nothing_and_ignores_an_old_count(harness, tmp_path):
    g = Guard(harness, tmp_path, enabled=False)
    old = "2 9 boot-a 999999\n"
    g.state_file().write_text(old)
    for _ in range(8):
        g.ask("0")
    g.ask("123456").ask("", "attempt").ask("", "success")
    assert g.run() == ["FAIL 0 0"] * 8 + ["OK", "COUNTED 0 0", "OK"]
    assert g.state_file().read_text() == old


def test_network_counter_counts_even_with_the_wait_off(harness, tmp_path):
    g = Guard(harness, tmp_path, enabled=False)
    for _ in range(6):
        g.ask("0", "network")
    g.ask("123456", "network")          # a wait runs for the network login
    g.ask("123456")                     # the phone itself is not locked by it
    g.ask("0")
    out = g.run()
    assert out[5:] == ["FAIL 6 30", "WAIT 30", "OK", "FAIL 0 0"]
    assert g.state_file(network=True).exists()
    assert not g.state_file().exists()


def test_network_and_phone_counters_are_separate(harness, tmp_path):
    g = Guard(harness, tmp_path)
    for _ in range(6):
        g.ask("0", "network")
    g.ask("0")                          # the phone's count is its own
    g.ask("123456")                     # a stranger's tries over VNC do not lock the phone
    assert g.run()[5:] == ["FAIL 6 30", "FAIL 1 0", "OK"]


# ---- Who may ask ----

def test_another_user_is_denied_and_nothing_is_counted(harness, tmp_path):
    g = Guard(harness, tmp_path)
    g.ask("0", uid=BOB).ask("123456", uid=BOB)
    g.ask("", "attempt", uid=BOB).ask("", "success", uid=BOB).ask("0", "network", uid=BOB)
    g.ask("0")                          # alice herself: her first wrong one
    assert g.run() == ["DENIED"] * 5 + ["FAIL 1 0"]


def test_another_user_cannot_clear_a_running_wait(harness, tmp_path):
    g = Guard(harness, tmp_path)
    for _ in range(6):
        g.ask("0")
    g.ask("", "success", uid=BOB)
    g.ask("0")
    assert g.run()[6:] == ["DENIED", "WAIT 30"]


def test_root_may_check_any_user(harness, tmp_path):
    g = Guard(harness, tmp_path)
    g.ask("0", uid=ROOT).ask("123456", uid=ROOT).ask("654321", uid=ROOT, user="bob")
    assert g.run() == ["FAIL 1 0", "OK", "OK"]


@pytest.mark.parametrize("entry", ["!", "*", "!!", "-", "!$6$dsectestsalt$x", "*LK*"])
def test_locked_or_missing_hash_never_opens(harness, tmp_path, entry):
    g = Guard(harness, tmp_path)
    g.cmd(f"shadow alice {entry}")
    g.ask("").ask("123456").ask("!").ask("*")
    assert g.run() == ["FAIL 1 0", "FAIL 2 0", "FAIL 3 0", "FAIL 4 0"]


def test_locked_copy_of_the_right_hash_does_not_open(harness, tmp_path):
    # passwd -l puts "!" before the hash; the right PIN behind it must not open the account
    g = Guard(harness, tmp_path)
    g.ask("123456").cmd("lock alice").ask("123456")
    assert g.run() == ["OK", "FAIL 1 0"]


# ---- Malformed requests ----

@pytest.mark.parametrize("data", [
    b"",
    b"alice",                           # no NUL at all
    b"alice\0",                         # no password
    b"\0123456\0",                      # empty user
    b"mallory\0x\0",                    # unknown user
    b"alice\0x\0other\0",               # unknown kind
    b"alice\0x\0attempt\0",             # a pattern try carries no password
    b"alice\0x\0success\0",
    b"alice\0x\0\0",                    # empty kind
], ids=lambda d: repr(d))
def test_malformed_request_is_an_error_and_not_counted(harness, tmp_path, data):
    g = Guard(harness, tmp_path)
    g.raw(data).ask("0")
    assert g.run() == ["ERROR", "FAIL 1 0"]


def test_request_of_full_size(harness, tmp_path):
    # REQUEST_MAX bytes exactly, as read_client() hands them over: no overrun (sanitizers)
    data = b"alice\0" + b"9" * (1024 - 7) + b"\0"
    assert len(data) == 1024
    g = Guard(harness, tmp_path)
    # Cut at the limit before the password's NUL: malformed, and not counted
    g.raw(data).raw(b"alice\0" + b"9" * 1018).ask("0")
    assert g.run() == ["FAIL 1 0", "ERROR", "FAIL 2 0"]


def test_extra_fields_after_the_kind_are_ignored(harness, tmp_path):
    g = Guard(harness, tmp_path)
    g.raw(b"alice\0" + b"\0attempt\0junk\0more")
    assert g.run() == ["COUNTED 1 0"]


# ---- Pattern tries ("attempt"/"success") ----

def test_attempt_counts_in_advance_and_success_takes_it_back(harness, tmp_path):
    g = Guard(harness, tmp_path)
    g.ask("", "attempt").ask("", "attempt").ask("", "success").ask("", "attempt")
    assert g.run() == ["COUNTED 1 0", "COUNTED 2 0", "OK", "COUNTED 1 0"]


def test_attempt_during_a_wait_is_not_counted(harness, tmp_path):
    g = Guard(harness, tmp_path)
    for _ in range(6):
        g.ask("", "attempt")
    for _ in range(3):
        g.ask("", "attempt")
    g.clock(1030).ask("", "attempt")
    assert g.run()[5:] == ["COUNTED 6 30", "WAIT 30", "WAIT 30", "WAIT 30", "COUNTED 7 60"]


def test_success_by_the_user_ends_a_wait(harness, tmp_path):
    # Documented in plinius-pinguard.h: the pattern is checked by the caller, who is the user
    g = Guard(harness, tmp_path)
    for _ in range(6):
        g.ask("0")
    g.ask("", "success").ask("0")
    assert g.run()[5:] == ["FAIL 6 30", "OK", "FAIL 1 0"]


# ---- Restarts ----

@pytest.mark.parametrize("old,answer", [
    ("2 7 boot-old 5\n", "WAIT 60"),    # another boot: the wait starts over in full
    ("7 12345\n", "WAIT 60"),           # the first format, epoch seconds
    ("2 7 boot-a 5\n", "FAIL 8 300"),   # this boot, deadline long over
    ("2 3 boot-old 0\n", "FAIL 4 0"),   # no wait to start over
    ("garbage\n", "FAIL 1 0"),
    ("", "FAIL 1 0"),
    # Without the deadline the line reads as the first format: 2 failures, nothing to wait for
    ("2 6 boot-old\n", "FAIL 3 0"),
])
def test_state_after_a_restart(harness, tmp_path, old, answer):
    g = Guard(harness, tmp_path)
    g.state_file().write_text(old)
    g.ask("0")
    assert g.run() == [answer]


def test_restarted_wait_is_written_back_once(harness, tmp_path):
    g = Guard(harness, tmp_path)
    g.state_file().write_text("2 7 boot-old 5\n")
    g.ask("0").clock(1010).ask("0")
    # The second try does not start the wait over again
    assert g.run() == ["WAIT 60", "WAIT 50"]
    assert g.state_file().read_text() == "2 7 boot-a 1060\n"


def test_no_password_in_the_log(harness, tmp_path):
    g = Guard(harness, tmp_path)
    for _ in range(7):
        g.ask("s3cr3t-pin")
    g.ask("0", uid=BOB)
    r = subprocess.run([str(harness)], input="\n".join(g.cmds) + "\n", capture_output=True,
                       text=True, timeout=30, env=san_env())
    assert r.returncode == 0, r.stderr
    assert "SYSLOG: wrong password for alice" in r.stderr
    assert "SYSLOG: uid 1002 asked for the password of alice" in r.stderr
    assert "s3cr3t" not in r.stderr and "s3cr3t" not in r.stdout


# ---- Tries from another account ("from:UID", only from root) ----

def from_file(g, uid=ALICE, other=BOB):
    return g.state / f"{uid}.from-{other}"


def test_try_from_another_account_has_its_own_counter(harness, tmp_path):
    # su or polkit's helper started by bob, for alice's PIN: counted per pair of accounts
    g = Guard(harness, tmp_path)
    for _ in range(6):
        g.ask("0", f"from:{BOB}", uid=ROOT)
    g.ask("123456", f"from:{BOB}", uid=ROOT)    # a wait runs for bob's tries
    g.ask("123456")                            # alice at her own lock screen is not locked out
    out = g.run()
    assert out[5:] == ["FAIL 6 30", "WAIT 30", "OK"]
    assert from_file(g).read_text() == "2 6 boot-a 1030\n"
    assert not g.state_file().exists()


def test_try_from_another_account_counts_even_with_the_wait_off(harness, tmp_path):
    g = Guard(harness, tmp_path, enabled=False)
    for _ in range(6):
        g.ask("0", f"from:{BOB}", uid=ROOT)
    g.ask("0")                                 # alice's own tries: wait off
    assert g.run()[5:] == ["FAIL 6 30", "FAIL 0 0"]
    assert from_file(g).exists() and not g.state_file().exists()


def test_try_from_the_user_itself_is_a_try_at_the_phone(harness, tmp_path):
    g = Guard(harness, tmp_path)
    g.ask("0", f"from:{ALICE}", uid=ROOT).ask("0")
    assert g.run() == ["FAIL 1 0", "FAIL 2 0"]
    assert not list(g.state.glob("*.from-*"))


def test_only_root_may_say_where_a_try_comes_from(harness, tmp_path):
    # Else any account could file its tries under a counter of its choice
    g = Guard(harness, tmp_path)
    g.ask("0", f"from:{BOB}").ask("0", f"from:{BOB}", uid=BOB).ask("0")
    assert g.run() == ["DENIED", "DENIED", "FAIL 1 0"]
    assert not list(g.state.glob("*.from-*"))


@pytest.mark.parametrize("kind,answer", [
    ("from:0", "ERROR"),                # root is never sent
    ("from:", "ERROR"),
    ("from:012", "ERROR"),              # one way to write each uid
    ("from:-1", "ERROR"),
    ("from:+5", "ERROR"),
    ("from:12x", "ERROR"),
    ("from:12345678901", "ERROR"),      # 11 digits
    ("from:2147483647", "ERROR"),       # 2^31 - 1 and above
    ("from:2147483646", "FAIL 1 0"),
    ("from:1002", "FAIL 1 0"),
])
def test_uid_of_a_try_from_another_account(harness, tmp_path, kind, answer):
    g = Guard(harness, tmp_path)
    g.ask("0", kind, uid=ROOT)
    assert g.run() == [answer]


def test_log_names_the_other_account_but_not_the_pin(harness, tmp_path):
    g = Guard(harness, tmp_path)
    g.ask("s3cr3t", f"from:{BOB}", uid=ROOT).ask("0", f"from:{BOB}", uid=BOB)
    r = subprocess.run([str(harness)], input="\n".join(g.cmds) + "\n", capture_output=True,
                       text=True, timeout=30, env=san_env())
    assert r.returncode == 0, r.stderr
    assert "SYSLOG: wrong password for alice from uid 1002, 1 in a row" in r.stderr
    assert "SYSLOG: uid 1002 asked for the password of alice" in r.stderr
    assert "s3cr3t" not in r.stderr


# ---- A counter that cannot be written ----

def test_try_that_cannot_be_counted_is_not_checked(harness, tmp_path):
    # Without its counter a try must not be answered: else hanging up would be free
    g = Guard(harness, tmp_path)
    g.cmd(f"dirs {tmp_path / 'missing'} {g.marker}")
    g.ask("0").ask("123456").ask("", "attempt").ask("0", "network")
    # The right PIN still opens: nothing is there to clear
    assert g.run() == ["ERROR", "OK", "ERROR", "ERROR"]


def test_with_the_wait_off_nothing_needs_writing(harness, tmp_path):
    g = Guard(harness, tmp_path, enabled=False)
    g.cmd(f"dirs {tmp_path / 'missing'} {g.marker}")
    g.ask("0").ask("123456")
    assert g.run() == ["FAIL 0 0", "OK"]


def test_failed_rename_leaves_no_temporary_file(harness, tmp_path):
    g = Guard(harness, tmp_path)
    # The counter's place is taken by a directory: the new counter cannot be renamed there
    (g.state_file() / "x").mkdir(parents=True)
    g.ask("0")
    assert g.run() == ["ERROR"]
    assert not list(g.state.glob("*.tmp"))
    r = subprocess.run([str(harness)], input="\n".join(g.cmds) + "\n", capture_output=True,
                       text=True, timeout=30, env=san_env())
    assert f"SYSLOG: cannot write {g.state_file()}" in r.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a read-only directory")
def test_success_that_cannot_clear_the_count_is_an_error(harness, tmp_path):
    g = Guard(harness, tmp_path)
    g.state_file().write_text("2 3 boot-a 0\n")
    g.state.chmod(0o500)
    try:
        g.ask("", "success")
        out = g.run()
    finally:
        g.state.chmod(0o700)
    # The pattern was right, but the count stays: the caller is told, not "OK"
    assert out == ["ERROR"]
    assert g.state_file().read_text() == "2 3 boot-a 0\n"


def test_success_without_a_count_writes_nothing(harness, tmp_path):
    g = Guard(harness, tmp_path)
    g.ask("", "success")
    assert g.run() == ["OK"]
    assert not g.state_file().exists()


def test_no_memory_for_the_check_never_opens(harness, tmp_path):
    g = Guard(harness, tmp_path)
    g.cmd("oom").ask("123456").ask("123456")
    assert g.run() == ["FAIL 1 0", "OK"]


def test_wait_ends_even_without_a_boot_id(harness, tmp_path):
    g = Guard(harness, tmp_path)
    g.cmd("nobootid")
    for _ in range(6):
        g.ask("0")
    g.clock(1010).ask("0").clock(1030).ask("123456")
    # The wait runs down (not over in full at every try) and ends
    assert g.run()[5:] == ["FAIL 6 30", "WAIT 20", "OK"]


@pytest.mark.parametrize("old,answer", [
    ("2 7 - 1030\n", "WAIT 30"),       # without boot id either: ends no later than a full wait
    ("2 7 - 99999\n", "WAIT 60"),      # later than a full wait could: an earlier, longer boot
    ("2 7 boot-old 1030\n", "WAIT 60"),  # a boot id then, none now: started over
    ("2 7 - 5\n", "FAIL 8 300"),       # over already
])
def test_counter_without_a_boot_id(harness, tmp_path, old, answer):
    g = Guard(harness, tmp_path)
    g.state_file().write_text(old)
    g.cmd("nobootid").ask("0")
    assert g.run() == [answer]


# ---- Client helpers ----

def test_client_refuses_an_overlong_request(harness, tmp_path):
    assert run_harness(harness, f"asklong {tmp_path / 'none'}\n") == ["err 22"]


@pytest.mark.parametrize("cmd,err", [("asknosock", 24), ("asknomem", 12)])
def test_client_without_resources_is_no_fallback(harness, tmp_path, cmd, err):
    # EMFILE / ENOMEM: the PAM module must not take them for a missing daemon (pam_unix)
    sock = tmp_path / "s"
    with FakeUnixServer(sock, ["OK"]) as srv:
        assert run_harness(harness, f"{cmd} {sock}\n") == [f"err {err}"]
        assert run_harness(harness, f"unreachable {err}\n") == ["no"]
    assert srv.requests == ([b""] if cmd == "asknomem" else [])


def test_client_sees_a_missing_daemon(harness, tmp_path):
    assert run_harness(harness, f"ask {tmp_path / 'none'}\n") == ["err 2"]


def test_client_reads_one_line_only(harness, tmp_path):
    path = tmp_path / "s"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)
    proc = subprocess.Popen([str(harness)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, env=san_env())
    proc.stdin.write(f"ask {path}\n")
    proc.stdin.close()
    conn, _ = srv.accept()
    with conn:
        data = b""
        while not data.endswith(b"x\0"):
            data += conn.recv(64)
        conn.sendall(b"FAIL 1 0\nOK\n")
    out = proc.stdout.read()
    proc.wait(timeout=10)
    srv.close()
    assert data == b"alice\0x\0"
    assert out.splitlines() == ["ok FAIL 1 0"]


# ---- The daemon process ----

def test_daemon_keeps_counters_private(tmp_path):
    daemon = build(tmp_path, "plinius-pinguard", [PINGUARD / "plinius-pinguard.c"],
                   flags=["-include", str(CDIR / "dsec_nolog.h"), "-lcrypt"])
    sock, state, marker = tmp_path / "socket", tmp_path / "state", tmp_path / "enabled"
    state.mkdir()
    marker.touch()
    proc = subprocess.Popen([str(daemon), "--listen", str(sock), "--state", str(state),
                             "--enabled", str(marker), "--boot-id", "boot-a"],
                            stderr=subprocess.PIPE, env=san_env())
    try:
        for _ in range(250):
            if sock.exists():
                break
            time.sleep(0.02)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(sock))
        user = pwd.getpwuid(os.getuid()).pw_name
        c.sendall(user.encode() + b"\0\0attempt\0")
        c.shutdown(socket.SHUT_WR)
        answer = c.recv(64)
        c.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert answer == b"COUNTED 1 0\n"
    counter = state / str(os.getuid())
    # umask 077 in the daemon: the counter is root's alone on the phone
    assert stat.S_IMODE(counter.stat().st_mode) == 0o600
    # The socket is for everyone (the lock screen runs as the user)
    assert stat.S_IMODE(sock.stat().st_mode) == 0o666
