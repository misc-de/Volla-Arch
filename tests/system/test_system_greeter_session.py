# plinius-greeter-session (greetd's default_session as the user greeter): one login account is
# logged in at once, several get phoc with the greeter, and a greeter that does not come up is
# replaced by the account used last. Only a copy with its paths in tmp_path is run; the account
# list comes from a stand-in plinius_greeter_accounts, phoc, systemd-cat, dbus-run-session, sleep
# and date are stubs.

import pytest

from conftest import PKG
from system_helpers import rewritten_copy, run

SRC = PKG / "plinius-greeter" / "plinius-greeter-session"

FAKE_ACCOUNTS = '''\
import os
import types


def login_users():
    names = [n for n in os.environ.get("FAKE_USERS", "").split(",") if n]
    return [types.SimpleNamespace(pw_name=n) for n in names]
'''


class Session:
    def __init__(self, tmp_path, fake_bin):
        self.tmp = tmp_path
        self.bin = fake_bin
        self.state = tmp_path / "run" / "plinius-greeter"
        self.last = tmp_path / "var" / "lib" / "plinius-greeter" / "last-user"
        self.lib = tmp_path / "lib" / "plinius-greeter"
        self.profile = tmp_path / "etc" / "profile"
        self.generator = tmp_path / "environment-d-generator"
        self.etc_ini = tmp_path / "etc" / "phosh" / "phoc.ini"
        self.share_ini = tmp_path / "share" / "phoc.ini"
        self.runtime = tmp_path / "xdg-runtime"
        for d in (self.state, self.last.parent, self.lib, self.profile.parent, self.runtime):
            d.mkdir(parents=True)
        (self.lib / "plinius_greeter_accounts.py").write_text(FAKE_ACCOUNTS)
        self.script = rewritten_copy(SRC, tmp_path / "plinius-greeter-session", {
            "/run/plinius-greeter": self.state,
            "/run/plinius-login/first-start": tmp_path / "first-start",
            "/var/lib/plinius-greeter/last-user": self.last,
            "/usr/lib/plinius-greeter": self.lib,
            "/etc/profile": self.profile,
            "/usr/lib/systemd/user-environment-generators/30-systemd-environment-d-generator":
                self.generator,
            "/etc/phosh/phoc.ini": self.etc_ini,
            "/usr/share/phosh/phoc.ini": self.share_ini,
        })
        self.env_out = tmp_path / "phoc-env"
        b = fake_bin
        b.stub("sleep")
        b.stub("systemd-cat", '[ "$1" = -t ] && shift 2; exec "$@"')
        b.stub("dbus-run-session", '[ "$1" = -- ] && shift; exec "$@"')
        # phoc: notes the environment the greeter gets, ends with PHOC_RC
        b.stub("phoc", f'env > "{self.env_out}"; exit "${{PHOC_RC:-0}}"')
        # date +%s: START before phoc, START + ELAPSED after it
        counter = tmp_path / "date-count"
        b.stub("date", f'n=$(cat "{counter}" 2>/dev/null || echo 0); echo $((n + 1)) > "{counter}"\n'
               f'if [ "$n" = 0 ]; then echo 5000; else echo $((5000 + ${{ELAPSED:-2}})); fi')

    def run(self, users, **env):
        e = self.bin.env(isolated=True, FAKE_USERS=",".join(users),
                         XDG_RUNTIME_DIR=self.runtime, **env)
        for key in ("DBUS_SESSION_BUS_ADDRESS", "WLR_BACKENDS", "GSETTINGS_BACKEND",
                    "XDG_CURRENT_DESKTOP"):
            if key not in env:
                e.pop(key, None)
        return run([self.script], e)

    def request(self):
        f = self.state / "request"
        return f.read_text() if f.exists() else None

    def phoc_env(self):
        return dict(line.split("=", 1) for line in self.env_out.read_text().splitlines()
                    if "=" in line)

    def failures(self):
        f = self.state / "failures"
        return int(f.read_text()) if f.exists() else 0


@pytest.fixture
def session(tmp_path, fake_bin):
    return Session(tmp_path, fake_bin)


# ----- one account -----

def test_greeter_session_single_account_is_logged_in_at_once(session):
    r = session.run(["alice"])
    assert r.returncode == 0, r.stderr
    assert session.request() == "alice\n"
    assert not (session.state / "request.tmp").exists()
    assert "alice is the only account, logging it in" in r.stderr
    # It waits for greetd to restart instead of drawing anything
    assert session.bin.calls("sleep") == [["60"]]
    assert session.bin.calls("phoc") == []


def test_greeter_session_single_account_without_password_gets_the_greeter(session):
    # plinius-greeter-config noted it: the greeter asks for its first PIN or password
    (session.tmp / "first-start").write_text("alice\n")
    r = session.run(["alice"])
    assert r.returncode == 0, r.stderr
    assert session.request() is None
    assert len(session.bin.calls("phoc")) == 1


def test_greeter_session_no_account_still_shows_the_greeter(session):
    r = session.run([])
    assert r.returncode == 0, r.stderr
    assert session.request() is None
    assert len(session.bin.calls("phoc")) == 1


# ----- several accounts: phoc with the greeter -----

def test_greeter_session_starts_phoc_with_the_greeter(session):
    r = session.run(["alice", "bob"])
    assert r.returncode == 0, r.stderr
    assert session.request() is None
    assert session.bin.calls("sleep") == []
    assert session.bin.calls("systemd-cat") == [[
        "-t", "plinius-greeter", "dbus-run-session", "--", "phoc", "-C", str(session.share_ini),
        "-E", str(session.lib / "plinius-greeter")]]
    env = session.phoc_env()
    assert env["HOME"] == str(session.runtime)
    assert env["GSETTINGS_BACKEND"] == "memory"
    assert env["XDG_CURRENT_DESKTOP"] == "Phosh:GNOME"
    assert env["WLR_BACKENDS"] == "drm,libinput"
    assert session.failures() == 0


def test_greeter_session_prefers_the_phoc_ini_in_etc(session):
    session.etc_ini.parent.mkdir(parents=True)
    session.etc_ini.write_text("[core]\n")
    assert session.run(["alice", "bob"]).returncode == 0
    assert session.bin.calls("phoc")[0][:2] == ["-C", str(session.etc_ini)]


def test_greeter_session_keeps_a_session_bus_and_backends(session):
    r = session.run(["alice", "bob"], DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
                    WLR_BACKENDS="headless")
    assert r.returncode == 0, r.stderr
    assert session.bin.calls("dbus-run-session") == []
    assert session.bin.calls("systemd-cat")[0][2] == "phoc"
    assert session.phoc_env()["WLR_BACKENDS"] == "headless"


def test_greeter_session_home_without_a_runtime_dir(session):
    e = session.bin.env(isolated=True, FAKE_USERS="alice,bob")
    for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        e.pop(key, None)
    assert run([session.script], e).returncode == 0
    assert session.phoc_env()["HOME"] == "/tmp"


def test_greeter_session_reads_profile_and_environment_d(session):
    session.profile.write_text('export FROM_PROFILE="profile.d was here"\n')
    marker = session.tmp / "pwned"
    # environment.d values may hold spaces and quotes; none of it may run as a command
    session.generator.write_text(
        "#!/bin/sh\n"
        "echo 'GSK_RENDERER=gl'\n"
        "echo 'SPACED=a b  c'\n"
        f"echo \"QUOTED=it's a'; touch {marker}; '\"\n"
        f"echo 'DOLLAR=$(touch {marker})`touch {marker}`'\n"
        f"echo 'BAD-KEY=1; touch {marker}'\n"
        f"echo '1NUM=x'\n"
        "echo 'no equals sign'\n")
    session.generator.chmod(0o755)
    r = session.run(["alice", "bob"])
    assert r.returncode == 0, r.stderr
    assert not marker.exists()
    env = session.phoc_env()
    assert env["FROM_PROFILE"] == "profile.d was here"
    assert env["GSK_RENDERER"] == "gl"
    assert env["SPACED"] == "a b  c"
    assert env["QUOTED"] == f"it's a'; touch {marker}; '"
    assert env["DOLLAR"] == f"$(touch {marker})`touch {marker}`"
    assert "BAD-KEY" not in env and "1NUM" not in env


def test_greeter_session_without_profile_or_generator(session):
    # Neither exists: no error, phoc still starts
    r = session.run(["alice", "bob"])
    assert r.returncode == 0 and r.stderr == ""


# ----- a greeter that does not come up -----

def test_greeter_session_counts_quick_failures(session):
    r = session.run(["alice", "bob"], PHOC_RC=1, ELAPSED=3)
    assert r.returncode == 1
    assert session.failures() == 1
    assert "phoc ended with 1 after 3 s (failure 1)" in r.stderr
    assert session.request() is None
    session.run(["alice", "bob"], PHOC_RC=1)
    assert session.failures() == 2
    assert session.request() is None


@pytest.mark.parametrize("rc, elapsed", [
    (0, 1),      # a clean end
    (143, 1),    # a signal: greetd restarting for the picked account
    (128, 1),
    (1, 10),     # it ran for a while
    (1, 600),
])
def test_greeter_session_not_a_failure(session, rc, elapsed):
    (session.state / "failures").write_text("2\n")
    r = session.run(["alice", "bob"], PHOC_RC=rc, ELAPSED=elapsed)
    assert r.returncode == rc
    assert session.failures() == 2
    assert session.request() is None


def test_greeter_session_third_failure_logs_in_the_last_account(session):
    (session.state / "failures").write_text("2\n")
    session.last.write_text("bob\n")
    r = session.run(["alice", "bob"], PHOC_RC=1, ELAPSED=0)
    assert r.returncode == 1
    assert session.failures() == 3
    assert session.request() == "bob\n"
    assert "logging in bob instead" in r.stderr


def test_greeter_session_failure_without_last_account_takes_the_first(session):
    (session.state / "failures").write_text("5\n")
    r = session.run(["carol", "alice", "bob"], PHOC_RC=2)
    assert r.returncode == 2
    assert session.failures() == 6
    # The first login account by user ID (the order login_users gives), the phone's first user
    assert session.request() == "carol\n"


def test_greeter_session_failure_with_an_empty_last_file(session):
    (session.state / "failures").write_text("2\n")
    session.last.write_text("")
    session.run(["alice", "bob"], PHOC_RC=1)
    assert session.request() == "alice\n"


def test_greeter_session_failure_without_any_account_requests_nothing(session):
    (session.state / "failures").write_text("2\n")
    r = session.run([], PHOC_RC=1)
    assert r.returncode == 1
    assert session.request() is None
    assert "instead" not in r.stderr
