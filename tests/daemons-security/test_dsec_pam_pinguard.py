# pam_plinius_pinguard: what the PAM module makes of the daemon's answers, and when it may fall
# back to pam_unix (only without a daemon at all). pam_sm_authenticate() runs in a harness with a
# fake libpam (c/dsec_fakepam.h) against a fake daemon on a socket in tmp_path.

import subprocess

import pytest

from dsec_helpers import CDIR, FakeUnixServer, build, refused_socket, san_env

PATTERN_PREFIX = "\x01plinius-pattern:"
PAM_SILENT = 0x8000
NULL_TOKEN = object()      # pam_get_authtok succeeds without a token


@pytest.fixture(scope="module")
def module(tmp_path_factory):
    return build(tmp_path_factory.mktemp("pam-pinguard"), "pam-pinguard-harness",
                 [CDIR / "dsec_pam_pinguard_harness.c"])


def authenticate(exe, sock, user="alice", token="123456", flags=0, extra=(), **env):
    tok = "-" if token is None else "=" if token is NULL_TOKEN else token.encode().hex()
    args = [str(exe), user, tok, str(flags), f"socket={sock}", *extra]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60, env=san_env(**env))
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    rc = [l[3:] for l in lines if l.startswith("rc ")]
    msgs = [l[4:] for l in lines if l.startswith("msg ")]
    delays = [l[6:] for l in lines if l.startswith("delay ")]
    return rc[0], msgs, delays


@pytest.mark.parametrize("answer,rc,message", [
    ("OK", "SUCCESS", None),
    ("FAIL 1 0", "AUTH_ERR", None),
    ("FAIL 6 30", "AUTH_ERR", "Wrong PIN. Next try in 30 seconds."),
    ("FAIL 8 300", "AUTH_ERR", "Wrong PIN. Next try in 5 minutes."),
    ("WAIT 125", "MAXTRIES", "Too many wrong PINs. Next try in 3 minutes."),
    ("WAIT 3600", "MAXTRIES", "Too many wrong PINs. Next try in 1 hour."),
    ("DENIED", "PERM_DENIED", None),
    ("ERROR", "AUTH_ERR", None),
    ("garbage", "AUTH_ERR", None),
    ("FAIL x y", "AUTH_ERR", None),
    ("", "AUTH_ERR", None),
])
def test_answers_of_the_daemon(module, tmp_path, answer, rc, message):
    sock = tmp_path / "s"
    with FakeUnixServer(sock, [answer]) as srv:
        got, msgs, delays = authenticate(module, sock)
    assert got == rc
    assert msgs == ([message] if message else [])
    # The fixed delay of a wrong PIN, also with the wait off (the stack ends here)
    assert delays == ["2000000"]
    assert srv.requests == [b"alice\x00123456\x00"]


def test_silent_flag_hides_the_messages(module, tmp_path):
    sock = tmp_path / "s"
    with FakeUnixServer(sock, ["FAIL 6 30", "WAIT 30", None]):
        assert authenticate(module, sock, flags=PAM_SILENT)[:2] == ("AUTH_ERR", [])
        assert authenticate(module, sock, flags=PAM_SILENT)[:2] == ("MAXTRIES", [])
        assert authenticate(module, sock, flags=PAM_SILENT)[:2] == ("AUTH_ERR", [])


def test_missing_daemon_falls_back_to_pam_unix(module, tmp_path):
    assert authenticate(module, tmp_path / "none")[0] == "AUTHINFO_UNAVAIL"


def test_refused_socket_falls_back_to_pam_unix(module, tmp_path):
    s = refused_socket(tmp_path / "s")
    try:
        assert authenticate(module, tmp_path / "s")[0] == "AUTHINFO_UNAVAIL"
    finally:
        s.close()


def test_daemon_without_answer_never_falls_back(module, tmp_path):
    # A daemon that is there but says nothing must not open the uncounted way of pam_unix
    sock = tmp_path / "s"
    with FakeUnixServer(sock, [None]):
        rc, msgs, _ = authenticate(module, sock)
    assert rc == "AUTH_ERR"
    assert msgs == ["The PIN could not be checked. Try again."]


def test_a_pattern_is_left_to_pam_plinius_pattern(module, tmp_path):
    sock = tmp_path / "s"
    with FakeUnixServer(sock, ["OK"]) as srv:
        rc, msgs, delays = authenticate(module, sock, token=PATTERN_PREFIX + "3:0.1.2.5.8.7")
    assert (rc, msgs, delays) == ("IGNORE", [], [])
    # Never sent to the daemon as a password
    assert srv.requests == []


def test_network_option_uses_the_network_counter(module, tmp_path):
    sock = tmp_path / "s"
    with FakeUnixServer(sock, ["FAIL 6 30"]) as srv:
        rc, msgs, _ = authenticate(module, sock, extra=["network"])
    assert rc == "AUTH_ERR"
    assert srv.requests == [b"alice\x00123456\x00network\x00"]


def test_no_token_is_a_failure(module, tmp_path):
    sock = tmp_path / "s"
    with FakeUnixServer(sock, ["OK"]) as srv:
        assert authenticate(module, sock, token=None)[0] == "AUTH_ERR"
    assert srv.requests == []


def test_unknown_user_from_pam(module, tmp_path):
    assert authenticate(module, tmp_path / "none", user="-")[0] == "USER_UNKNOWN"


def test_overlong_pin_is_no_fallback(module, tmp_path):
    # Too long for one request: EINVAL, which must not count as "no daemon"
    sock = tmp_path / "s"
    with FakeUnixServer(sock, ["OK"]) as srv:
        rc, msgs, _ = authenticate(module, sock, token="9" * 2000)
    assert rc == "AUTH_ERR"
    assert srv.requests == []


def test_success_without_a_token_is_a_failure(module, tmp_path):
    sock = tmp_path / "s"
    with FakeUnixServer(sock, ["OK"]) as srv:
        assert authenticate(module, sock, token=NULL_TOKEN)[0] == "AUTH_ERR"
    assert srv.requests == []


def run_raw(module, sock, *extra, **env):
    r = subprocess.run([str(module), "alice", b"0".hex(), "0", f"socket={sock}", *extra],
                       capture_output=True, text=True, timeout=60, env=san_env(**env))
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def test_unknown_option_is_logged_and_ignored(module, tmp_path):
    sock = tmp_path / "s"
    with FakeUnixServer(sock, ["FAIL 1 0"]) as srv:
        out = run_raw(module, sock, "bogus")
    assert "log unknown option: bogus" in out and out[-1] == "rc AUTH_ERR"
    assert srv.requests == [b"alice\x000\x00"]


@pytest.mark.parametrize("euid,ruid,kind", [
    ("0", "1002", b"from:1002\x00"),   # su or polkit's helper, started by uid 1002
    ("0", "1001", b"from:1001\x00"),   # started by the user itself: the daemon files it as usual
    ("0", "0", b""),                    # sudo, greetd: full root, a try at the phone
    ("1001", "1001", b""),              # the lock screen, as the user
    ("1001", "0", b""),
])
def test_setuid_program_says_who_started_it(module, tmp_path, euid, ruid, kind):
    sock = tmp_path / "s"
    with FakeUnixServer(sock, ["FAIL 1 0"]) as srv:
        assert authenticate(module, sock, DSEC_EUID=euid, DSEC_RUID=ruid)[0] == "AUTH_ERR"
    assert srv.requests == [b"alice\x00123456\x00" + kind]


def test_network_wins_over_a_setuid_caller(module, tmp_path):
    sock = tmp_path / "s"
    with FakeUnixServer(sock, ["OK"]) as srv:
        authenticate(module, sock, extra=["network"], DSEC_EUID="0", DSEC_RUID="1002")
    assert srv.requests == [b"alice\x00123456\x00network\x00"]


def test_setcred_does_nothing(module, tmp_path):
    assert run_raw(module, tmp_path / "none", DSEC_SETCRED="1") == ["rc SUCCESS"]
