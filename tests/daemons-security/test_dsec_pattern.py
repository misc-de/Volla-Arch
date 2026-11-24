# plinius-pattern: the unlock pattern. pattern_parse() (plinius-pattern.h), the setup tool
# plinius-pattern with a home in tmp_path, pam_plinius_pattern with a fake libpam
# (c/dsec_fakepam.h), and the tool against the real plinius-pinguard daemon, all in tmp_path.

import os
import stat
import subprocess
import time

import pytest

from conftest import PKG
from dsec_helpers import CDIR, FakeUnixServer, build, san_env

PATTERN = PKG / "plinius-pattern"
PINGUARD = PKG / "plinius-pinguard"
PREFIX = "\x01plinius-pattern:"
RIGHT = "3:0.1.2.5.8.7"
WRONG = "3:0.1.2.5.8.6"


@pytest.fixture(scope="module")
def binaries(tmp_path_factory):
    d = tmp_path_factory.mktemp("pattern")
    return {
        "pam": build(d, "pam-pattern-harness", [CDIR / "dsec_pam_pattern_harness.c"],
                     flags=["-lcrypt"]),
        # The tool as it is; its default socket and the user database's home are the test's
        "tool": build(d, "pattern-tool", [CDIR / "dsec_pattern_tool_harness.c"],
                      flags=["-lcrypt"]),
    }


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def tool(binaries, home, cmd, stdin="", sock=None, args=None, **extra):
    base = {"HOME": home, "PLINIUS_PINGUARD_SOCKET": sock or home.parent / "no-daemon",
            "DSEC_DEFAULT_SOCKET": home.parent / "no-default-daemon"}
    env = san_env(**{**base, **extra})
    return subprocess.run([str(binaries["tool"]), *([cmd] if args is None else args)],
                          input=stdin, capture_output=True, text=True, timeout=60, env=env)


def set_pattern(binaries, home, pattern=RIGHT):
    r = tool(binaries, home, "set", pattern + "\n")
    assert r.returncode == 0, r.stderr
    return home / ".config/plinius/unlock-pattern"


# ---- pattern_parse ----

@pytest.mark.parametrize("text,points,grid", [
    ("3:0.1.2", 3, 3),
    ("3:0.1.2.3.4.5.6.7.8", 9, 3),
    ("4:15", 1, 4),
    ("6:35.0", 2, 6),
    ("6:" + ".".join(str(i) for i in range(36)), 36, 6),
    ("3:9", 0, -1),           # point beyond the grid
    ("4:16", 0, -1),
    ("2:0.1", 0, -1),         # grid too small
    ("7:0", 0, -1),           # grid too large
    ("9:0", 0, -1),
    ("3:", 0, -1),
    ("3", 0, -1),
    ("", 0, -1),
    (":0", 0, -1),
    ("3:0..1", 0, -1),
    ("3:0.1.", 0, -1),
    ("3:.0", 0, -1),
    ("3:0.0", 0, -1),         # a point twice
    ("3:00", 0, -1),          # one way to write each point
    ("3:01", 0, -1),
    ("3:123", 0, -1),
    ("6:100", 0, -1),         # three digits
    ("3:-1", 0, -1),
    ("3:+1", 0, -1),
    (" 3:0", 0, -1),
    ("3:0 ", 0, -1),
    ("3:0;1", 0, -1),
    ("3:5a", 0, -1),
    ("33:0", 0, -1),
    ("6:" + "1" * 200, 0, -1),
    ("6:" + ".".join(["35"] * 60), 0, -1),
])
def test_pattern_parse(binaries, text, points, grid):
    r = subprocess.run([str(binaries["pam"]), "--parse"], input=text + "\n", capture_output=True,
                       text=True, timeout=30, env=san_env())
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == [str(points), str(grid)]


# ---- The tool ----

def test_set_needs_six_points(binaries, home):
    r = tool(binaries, home, "set", "3:0.1.2.5.8\n")
    assert r.returncode == 1
    assert not (home / ".config/plinius/unlock-pattern").exists()


def test_set_stores_grid_and_hash_privately(binaries, home):
    f = set_pattern(binaries, home)
    lines = f.read_text().splitlines()
    assert lines[0] == "3" and lines[1].startswith("$") and len(lines) == 2
    assert "0.1.2" not in f.read_text()
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    r = tool(binaries, home, "status")
    assert (r.returncode, r.stdout) == (0, "3\n")


def test_set_does_not_follow_a_planted_link(binaries, home, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    d = home / ".config/plinius"
    d.mkdir(parents=True)
    (d / "unlock-pattern.new").symlink_to(victim)
    r = tool(binaries, home, "set", RIGHT + "\n")
    assert r.returncode == 1
    assert victim.read_text() == "keep\n"


def test_check_without_daemon(binaries, home):
    set_pattern(binaries, home)
    assert tool(binaries, home, "check", RIGHT + "\n").returncode == 0
    assert tool(binaries, home, "check", WRONG + "\n").returncode == 1
    # The same drawing on another grid is another pattern
    assert tool(binaries, home, "check", "4:0.1.2.5.8.7\n").returncode == 1
    assert tool(binaries, home, "check", "garbage\n").returncode == 1
    assert tool(binaries, home, "check", "").returncode == 1


def test_check_counts_and_reports_success(binaries, home):
    set_pattern(binaries, home)
    sock = home.parent / "s"
    user = __import__("pwd").getpwuid(os.getuid()).pw_name.encode()
    with FakeUnixServer(sock, ["COUNTED 1 0", "OK", "COUNTED 2 0"]) as srv:
        assert tool(binaries, home, "check", RIGHT + "\n", sock).returncode == 0
        assert tool(binaries, home, "check", WRONG + "\n", sock).returncode == 1
    assert srv.requests == [user + b"\0\0attempt\0", user + b"\0\0success\0",
                            user + b"\0\0attempt\0"]


def test_check_during_a_wait_is_not_checked(binaries, home):
    set_pattern(binaries, home)
    sock = home.parent / "s"
    with FakeUnixServer(sock, ["WAIT 30"]) as srv:
        r = tool(binaries, home, "check", RIGHT + "\n", sock)
    assert (r.returncode, r.stdout) == (3, "WAIT 30\n")
    assert len(srv.requests) == 1


def test_check_fails_when_the_daemon_does_not_answer(binaries, home):
    set_pattern(binaries, home)
    sock = home.parent / "s"
    with FakeUnixServer(sock, [None]):
        assert tool(binaries, home, "check", RIGHT + "\n", sock).returncode == 1


def test_remove(binaries, home):
    f = set_pattern(binaries, home)
    assert tool(binaries, home, "remove").returncode == 0
    assert not f.exists()
    assert tool(binaries, home, "status").returncode == 1
    assert tool(binaries, home, "remove").returncode == 0


def test_usage(binaries, home):
    assert tool(binaries, home, "other").returncode == 2


def test_usage_needs_exactly_one_command(binaries, home):
    for args in ([], ["set", "extra"]):
        r = tool(binaries, home, None, args=args)
        assert r.returncode == 2 and "usage: plinius-pattern" in r.stderr


def test_home_from_the_user_database_without_home(binaries, home):
    # HOME unset (or empty): the home of the user database, here the test's
    for value in (None, ""):
        env = {"HOME": value} if value is not None else {}
        r = subprocess.run([str(binaries["tool"]), "set"], input=RIGHT + "\n",
                           capture_output=True, text=True, timeout=60,
                           env={**{k: v for k, v in san_env(DSEC_PW_HOME=home).items()
                                   if k != "HOME"}, **env})
        assert r.returncode == 0, r.stderr
        assert (home / ".config/plinius/unlock-pattern").exists()
        (home / ".config/plinius/unlock-pattern").unlink()


def test_without_home_and_user_entry_nothing_is_written(binaries, home):
    env = {k: v for k, v in san_env(DSEC_NO_USER=1).items() if k != "HOME"}
    for cmd in ("set", "status", "remove", "check"):
        r = subprocess.run([str(binaries["tool"]), cmd], input=RIGHT + "\n", capture_output=True,
                           text=True, timeout=60, env=env)
        assert r.returncode == 1, cmd


def test_overlong_home_is_refused(binaries, home):
    long_home = "/" + "h" * 4090
    assert tool(binaries, home, "set", RIGHT + "\n", HOME=long_home).returncode == 1
    assert tool(binaries, home, "remove", HOME=long_home).returncode == 1
    assert tool(binaries, home, "status", HOME=long_home).returncode == 1


def test_set_reports_a_home_it_cannot_write(binaries, home):
    r = tool(binaries, home, "set", RIGHT + "\n", HOME=home / "missing")
    assert r.returncode == 1
    assert f"plinius-pattern: {home}/missing/.config/plinius:" in r.stderr


def test_set_fails_when_the_file_cannot_be_replaced(binaries, home):
    # The file's place is taken by a directory: the old state stays, no half-written file
    target = home / ".config/plinius/unlock-pattern"
    (target / "x").mkdir(parents=True)
    r = tool(binaries, home, "set", RIGHT + "\n")
    assert r.returncode == 1 and f"{target}:" in r.stderr
    assert not (home / ".config/plinius/unlock-pattern.new").exists()
    assert target.is_dir()


def test_set_and_check_without_memory(binaries, home):
    r = tool(binaries, home, "set", RIGHT + "\n", DSEC_NO_MEMORY=1)
    assert r.returncode == 1 and "hashing failed" in r.stderr
    assert not (home / ".config/plinius/unlock-pattern").exists()
    set_pattern(binaries, home)
    # The right pattern is not taken when it cannot be checked
    assert tool(binaries, home, "check", RIGHT + "\n", DSEC_NO_MEMORY=1).returncode == 1


@pytest.mark.parametrize("content", ["3\n", "3", "9\n$y$x\n", "2\n$y$x\n", "3\n\n"])
def test_saved_file_that_is_not_a_pattern(binaries, home, content):
    f = home / ".config/plinius/unlock-pattern"
    f.parent.mkdir(parents=True)
    f.write_text(content)
    assert tool(binaries, home, "status").returncode == 1
    assert tool(binaries, home, "check", RIGHT + "\n").returncode == 1


def test_check_uses_the_default_daemon(binaries, home):
    set_pattern(binaries, home)
    sock = home.parent / "default"
    user = __import__("pwd").getpwuid(os.getuid()).pw_name.encode()
    with FakeUnixServer(sock, ["COUNTED 1 0", "OK"]) as srv:
        r = tool(binaries, home, "check", RIGHT + "\n", PLINIUS_PINGUARD_SOCKET="",
                 DSEC_DEFAULT_SOCKET=sock)
    assert r.returncode == 0
    assert srv.requests == [user + b"\0\0attempt\0", user + b"\0\0success\0"]


def test_check_without_a_user_entry_is_not_counted(binaries, home):
    set_pattern(binaries, home)
    sock = home.parent / "s"
    with FakeUnixServer(sock, ["COUNTED 1 0"]) as srv:
        assert tool(binaries, home, "check", RIGHT + "\n", sock, DSEC_NO_USER=1).returncode == 0
    assert srv.requests == []


@pytest.mark.parametrize("second", [None, "ERROR"])
def test_right_pattern_whose_success_is_not_taken(binaries, home, second):
    # Still right; the try stays counted, and the tool says so
    set_pattern(binaries, home)
    sock = home.parent / "s"
    with FakeUnixServer(sock, ["COUNTED 1 0", second]):
        r = tool(binaries, home, "check", RIGHT + "\n", sock)
    assert r.returncode == 0
    assert "plinius-pinguard did not take the right pattern" in r.stderr


def test_remove_reports_what_it_cannot_remove(binaries, home):
    target = home / ".config/plinius/unlock-pattern"
    (target / "x").mkdir(parents=True)
    r = tool(binaries, home, "remove")
    assert r.returncode == 1 and f"{target}:" in r.stderr


# ---- The PAM module ----

def pam(binaries, home, token, sock=None, user="alice", flags=0, uid=None, extra=(), **env_extra):
    env = san_env(**{"DSEC_HOME": home, **env_extra})
    if uid is not None:
        env["DSEC_UID"] = str(uid)
    args = [str(binaries["pam"]), user, token if token in ("-", "=") else token.encode().hex(),
            str(flags), f"socket={sock or home.parent / 'no-daemon'}", *extra]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    return ([l[3:] for l in lines if l.startswith("rc ")][0],
            [l[4:] for l in lines if l.startswith("msg ")],
            [l for l in lines if l.startswith("delay ")])


def test_pin_without_pattern_goes_on_to_pam_unix(binaries, home):
    sock = home.parent / "s"
    with FakeUnixServer(sock, ["COUNTED 1 0"]) as srv:
        assert pam(binaries, home, "123456", sock) == ("IGNORE", [], [])
    assert srv.requests == []


def test_pattern_without_pattern_file_fails_here(binaries, home):
    # The marked answer must never reach pam_unix as a password
    rc, msgs, delays = pam(binaries, home, PREFIX + RIGHT)
    assert rc == "AUTH_ERR" and delays == ["delay 2000000"]


def test_pin_is_refused_while_a_pattern_is_set(binaries, home):
    set_pattern(binaries, home)
    sock = home.parent / "s"
    with FakeUnixServer(sock, ["COUNTED 1 0"]) as srv:
        rc, _, delays = pam(binaries, home, "123456", sock)
    assert rc == "AUTH_ERR" and delays == ["delay 2000000"]
    assert srv.requests == []


def test_right_pattern_counted_and_cleared(binaries, home):
    set_pattern(binaries, home)
    sock = home.parent / "s"
    with FakeUnixServer(sock, ["COUNTED 1 0", "OK"]) as srv:
        assert pam(binaries, home, PREFIX + RIGHT, sock)[0] == "SUCCESS"
    assert srv.requests == [b"alice\0\0attempt\0", b"alice\0\0success\0"]


def test_right_pattern_when_success_is_not_taken(binaries, home):
    set_pattern(binaries, home)
    sock = home.parent / "s"
    with FakeUnixServer(sock, ["COUNTED 1 0", None]):
        assert pam(binaries, home, PREFIX + RIGHT, sock)[0] == "SUCCESS"


@pytest.mark.parametrize("answer,message", [
    ("COUNTED 2 0", None),
    ("COUNTED 6 30", "Wrong pattern. Next try in 30 seconds."),
    ("COUNTED 10 3600", "Wrong pattern. Next try in 1 hour."),
])
def test_wrong_pattern(binaries, home, answer, message):
    set_pattern(binaries, home)
    sock = home.parent / "s"
    with FakeUnixServer(sock, [answer]) as srv:
        rc, msgs, _ = pam(binaries, home, PREFIX + WRONG, sock)
    assert rc == "AUTH_ERR"
    assert msgs == ([message] if message else [])
    assert len(srv.requests) == 1          # no "success" for a wrong one


def test_right_pattern_during_a_wait(binaries, home):
    set_pattern(binaries, home)
    sock = home.parent / "s"
    with FakeUnixServer(sock, ["WAIT 90"]) as srv:
        rc, msgs, _ = pam(binaries, home, PREFIX + RIGHT, sock)
    assert rc == "MAXTRIES"
    assert msgs == ["Too many wrong tries. Next try in 2 minutes."]
    assert len(srv.requests) == 1


def test_silent_wait(binaries, home):
    set_pattern(binaries, home)
    sock = home.parent / "s"
    with FakeUnixServer(sock, ["WAIT 90"]):
        assert pam(binaries, home, PREFIX + RIGHT, sock, flags=0x8000)[:2] == ("MAXTRIES", [])


def test_daemon_without_answer_is_a_failed_try(binaries, home):
    set_pattern(binaries, home)
    sock = home.parent / "s"
    with FakeUnixServer(sock, [None]):
        rc, msgs, _ = pam(binaries, home, PREFIX + RIGHT, sock)
    assert rc == "MAXTRIES"
    assert msgs == ["The pattern could not be checked. Try again."]


def test_without_daemon_the_pattern_is_checked_uncounted(binaries, home):
    set_pattern(binaries, home)
    assert pam(binaries, home, PREFIX + RIGHT)[0] == "SUCCESS"
    assert pam(binaries, home, PREFIX + WRONG)[0] == "AUTH_ERR"


@pytest.mark.parametrize("answer", ["DENIED", "ERROR", "nonsense"])
def test_daemon_that_does_not_count_still_checks(binaries, home, answer):
    set_pattern(binaries, home)
    sock = home.parent / "s"
    with FakeUnixServer(sock, [answer, answer]) as srv:
        assert pam(binaries, home, PREFIX + RIGHT, sock)[0] == "SUCCESS"
        assert pam(binaries, home, PREFIX + WRONG, sock)[0] == "AUTH_ERR"
    # Nothing was counted, so no "success" is reported
    assert len(srv.requests) == 2


@pytest.mark.parametrize("pattern", [
    "4:0.1.2.5.8.7",    # another grid
    "3:0.0.1.2.5.8",    # malformed
    "",                 # the prefix alone
    "3:0.1.2.5.8.7.",
])
def test_not_the_pattern(binaries, home, pattern):
    set_pattern(binaries, home)
    sock = home.parent / "s"
    with FakeUnixServer(sock, ["COUNTED 1 0"]):
        assert pam(binaries, home, PREFIX + pattern, sock)[0] == "AUTH_ERR"


def broken_file(binaries, home, how, tmp_path):
    f = set_pattern(binaries, home)
    if how == "group-writable":
        f.chmod(0o620)
    elif how == "world-writable":
        f.chmod(0o602)
    elif how == "symlink":
        real = tmp_path / "elsewhere"
        f.rename(real)
        f.symlink_to(real)
    elif how == "directory":
        f.unlink()
        f.mkdir()
    elif how == "grid-9":
        f.write_text("9\n" + f.read_text().split("\n", 1)[1])
    elif how == "no-newline":
        f.write_text("3")
    elif how == "empty-hash":
        f.write_text("3\n\n")
    elif how == "empty":
        f.write_text("")


@pytest.mark.parametrize("how", ["group-writable", "world-writable", "symlink", "directory",
                                 "grid-9", "no-newline", "empty-hash", "empty"])
def test_file_the_module_does_not_accept(binaries, home, tmp_path, how):
    # Not ours: the PIN goes on to pam_unix (never locked out by a broken file), and a planted
    # or broken file never opens the screen with a pattern
    broken_file(binaries, home, how, tmp_path)
    assert pam(binaries, home, "123456")[0] == "IGNORE"
    assert pam(binaries, home, PREFIX + RIGHT)[0] == "AUTH_ERR"


def test_file_of_another_owner_is_not_accepted(binaries, home):
    set_pattern(binaries, home)
    other = os.getuid() + 1
    assert pam(binaries, home, "123456", uid=other)[0] == "IGNORE"
    assert pam(binaries, home, PREFIX + RIGHT, uid=other)[0] == "AUTH_ERR"


def test_unknown_user(binaries, home):
    assert pam(binaries, home, "123456", user="ghost")[0] == "IGNORE"
    assert pam(binaries, home, PREFIX + RIGHT, user="ghost")[0] == "AUTH_ERR"


def test_module_unknown_option_is_logged(binaries, home):
    r = subprocess.run([str(binaries["pam"]), "alice", b"123456".hex(), "0",
                        f"socket={home.parent / 'no-daemon'}", "bogus"],
                       capture_output=True, text=True, timeout=60, env=san_env(DSEC_HOME=home))
    assert "log unknown option: bogus" in r.stdout.splitlines()
    assert r.stdout.splitlines()[-1] == "rc IGNORE"


@pytest.mark.parametrize("user,token", [("-", PREFIX + RIGHT), ("alice", "-"), ("alice", "=")])
def test_module_without_user_or_answer_leaves_it_to_others(binaries, home, user, token):
    set_pattern(binaries, home)
    assert pam(binaries, home, token, user=user)[0] == "IGNORE"


def test_module_with_an_overlong_home(binaries, home):
    # No pattern file can be there: the PIN goes on, a pattern fails here
    assert pam(binaries, home, "123456", DSEC_HOME="/" + "h" * 4100)[0] == "IGNORE"


def test_module_without_memory_never_lets_in(binaries, home):
    set_pattern(binaries, home)
    sock = home.parent / "s"
    with FakeUnixServer(sock, ["COUNTED 1 0"]) as srv:
        rc, _, _ = pam(binaries, home, PREFIX + RIGHT, sock, DSEC_NO_MEMORY=1)
    assert rc == "BUF_ERR"
    assert len(srv.requests) == 1              # counted, and no "success" sent


def test_module_setcred_ignores(binaries, home):
    r = subprocess.run([str(binaries["pam"]), "alice", "-", "0"], capture_output=True,
                       text=True, timeout=60, env=san_env(DSEC_SETCRED=1))
    assert r.stdout.splitlines() == ["rc IGNORE"]


# ---- Against the real daemon ----

def test_pattern_tries_wait_like_pins(binaries, home, tmp_path):
    daemon = build(tmp_path, "pinguard-for-pattern", [PINGUARD / "plinius-pinguard.c"],
                   flags=["-include", str(CDIR / "dsec_nolog.h"), "-lcrypt"])
    sock, state, marker = tmp_path / "socket", tmp_path / "state", tmp_path / "enabled"
    state.mkdir()
    marker.touch()
    set_pattern(binaries, home)
    proc = subprocess.Popen([str(daemon), "--listen", str(sock), "--state", str(state),
                             "--enabled", str(marker), "--boot-id", "boot-a"],
                            stderr=subprocess.DEVNULL, env=san_env())
    try:
        for _ in range(250):
            if sock.exists():
                break
            time.sleep(0.02)
        # A right pattern clears what the wrong ones counted
        for _ in range(4):
            assert tool(binaries, home, "check", WRONG + "\n", sock).returncode == 1
        assert tool(binaries, home, "check", RIGHT + "\n", sock).returncode == 0
        assert (state / str(os.getuid())).read_text().split()[:2] == ["2", "0"]
        for _ in range(6):
            assert tool(binaries, home, "check", WRONG + "\n", sock).returncode == 1
        r = tool(binaries, home, "check", RIGHT + "\n", sock)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    # The sixth wrong one started a wait: even the right pattern is not checked now
    assert r.returncode == 3 and r.stdout.startswith("WAIT ")
