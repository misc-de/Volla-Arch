# plinius-greeter: the account picker in front of Phosh, and the root side behind it.
#
# The greeter never talks to greetd itself: a tap writes the account's name into its own run
# directory (request), "plinius-greeter-config login" (root) checks it, notes it as next-user and
# last-user, removes greetd's run file and restarts greetd; "plinius-greeter-config greetd" then
# writes greetd's configuration with that account as initial_session. Both sides are tested here
# with every path in tmp_path: the accounts rule (login.defs, shells, passwd), the TOML writer, the
# request checks, and the window's logic with GTK faked (tiles, auto-login of a single account,
# failed starts, the dark screen, the power key and its menu, the backlight through logind).

import json
import math
import os
import random
import shutil
import stat
import subprocess
import sys
import tomllib
import types

import gi
import pytest

gi.require_version("Pango", "1.0")  # the greeter imports it without a version
gi.require_version("Graphene", "1.0")
from gi.repository import Gio, GLib, Graphene  # noqa: E402

import desktop_fakes as fakes  # noqa: E402
from conftest import PKG, load_script  # noqa: E402

GREETER = PKG / "plinius-greeter"
POWER_KEY = 0x1008FF2A
ESCAPE_KEY = 0xFF1B


@pytest.fixture(scope="module")
def accounts_mod():
    # The scripts put /usr/lib/plinius-greeter first in sys.path: the repository's module is
    # imported before them, under the name they import, and sys.path is restored
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "path", list(sys.path))
        mod = load_script(GREETER / "plinius_greeter_accounts.py", "plinius_greeter_accounts")
    yield mod
    sys.modules.pop("plinius_greeter_accounts", None)


@pytest.fixture(scope="module")
def config_mod(accounts_mod):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "path", list(sys.path))
        mod = load_script(GREETER / "plinius-greeter-config", "plinius_greeter_config_under_test")
    assert mod.accounts is accounts_mod
    return mod


@pytest.fixture(scope="module")
def mod(accounts_mod):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "path", list(sys.path))
        m = fakes.load_gui(GREETER / "plinius-greeter", "plinius_greeter_under_test",
                           AccountsService=fakes.namespace("AccountsService"))
    assert m.accounts is accounts_mod
    return m


def entry(name, uid, shell="/bin/bash", gecos=""):
    return types.SimpleNamespace(pw_name=name, pw_uid=uid, pw_shell=shell, pw_gecos=gecos)


@pytest.fixture
def users(accounts_mod, tmp_path, monkeypatch):
    """Sets the passwd entries the accounts module sees; login.defs and shells in tmp_path."""
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    (etc / "login.defs").write_text("UID_MIN 1000\nUID_MAX 60000\n")
    (etc / "shells").write_text("/bin/bash\n/bin/sh\n/usr/bin/nologin\n")
    monkeypatch.setattr(accounts_mod, "LOGIN_DEFS", str(etc / "login.defs"))
    monkeypatch.setattr(accounts_mod, "SHELLS", str(etc / "shells"))
    current = []
    monkeypatch.setattr(accounts_mod, "pwd", types.SimpleNamespace(getpwall=lambda: list(current)))

    def set_users(*entries):
        current[:] = entries
    set_users.etc = etc
    return set_users


# ----- the accounts rule -----

def test_login_users_by_uid_range_and_shell(accounts_mod, users):
    users(entry("root", 0), entry("bob", 1001), entry("alice", 1000),
          entry("aid_gps", 1021, "/usr/bin/nologin"), entry("daemon", 2, "/bin/sh"),
          entry("ghost", 1002, "/bin/zsh"), entry("nobody", 65534),
          entry("carol", 60000), entry("dave", 60001))
    # By user ID; no system user, no nologin shell, no shell missing from /etc/shells
    assert [e.pw_name for e in accounts_mod.login_users()] == ["alice", "bob", "carol"]
    assert accounts_mod.is_login_user("bob")
    assert not accounts_mod.is_login_user("root")
    assert not accounts_mod.is_login_user("aid_gps")
    assert not accounts_mod.is_login_user("")


def test_login_users_duplicate_names_once(accounts_mod, users):
    # Two passwd entries with one name (NSS and files, say): the one with the lower ID counts
    users(entry("alice", 1005), entry("alice", 1000, "/bin/sh"))
    found = accounts_mod.login_users()
    assert [(e.pw_name, e.pw_uid) for e in found] == [("alice", 1000)]


def test_login_defs_values_and_defaults(accounts_mod, users):
    defs = users.etc / "login.defs"
    defs.write_text("# comment\nUID_MIN\t32000\nUID_MAX   junk\nGID_MIN 5\nUID_MIN\n")
    # UID_MAX unreadable: its default stays
    assert accounts_mod._login_defs() == (32000, 60000)
    defs.unlink()
    assert accounts_mod._login_defs() == (1000, 60000)
    users(entry("first", 1000), entry("second", 32012))
    defs.write_text("UID_MIN 32000\n")
    assert [e.pw_name for e in accounts_mod.login_users()] == ["second"]


def test_shells_comments_and_no_login_shells(accounts_mod, users):
    (users.etc / "shells").write_text("# /etc/shells\n\n /bin/bash \n/usr/bin/nologin\n"
                                      "/sbin/nologin\n/bin/false\n/usr/bin/fish\n")
    assert accounts_mod._shells() == {"/bin/bash", "/usr/bin/fish"}
    (users.etc / "shells").unlink()
    # Without /etc/shells nobody has a login shell
    assert accounts_mod._shells() == set()
    users(entry("alice", 1000))
    assert accounts_mod.login_users() == []


@pytest.mark.parametrize("gecos, name", [
    ("Alice Example,Room 1,,", "Alice Example"),
    ("", "alice"),
    (" ,x", "alice"),
])
def test_real_name(accounts_mod, gecos, name):
    assert accounts_mod.real_name(entry("alice", 1000, gecos=gecos)) == name


# ----- plinius-greeter-config: TOML -----

def test_toml_round_trip(config_mod):
    config = {
        "terminal": {"vt": 1, "switch": True, "other": False},
        "initial_session": {"command": 'env A="b c" \\path\nnext\té', "user": "alice",
                            "list": [1, "x", True, []]},
        "empty": {},
    }
    text = config_mod.toml_dump(config)
    # A bool is written as one, not as the int it also is
    assert "switch = true" in text and "other = false" in text
    assert tomllib.loads(text) == config


@pytest.mark.parametrize("value", [1.5, None, {"nested": 1}, (1, 2)])
def test_toml_refuses_what_it_cannot_write(config_mod, value):
    with pytest.raises(ValueError):
        config_mod.toml_dump({"s": {"k": value}})


def test_toml_top_level_must_be_tables(config_mod):
    with pytest.raises(ValueError, match="not a table"):
        config_mod.toml_dump({"vt": 1})


@pytest.mark.parametrize("command", [
    "env GREETING=\U0001F600 phosh-session", "caf\u00e9 \u4e2d\U00010348", "a\x00b\x1fc\x7fd\x08e\x0cf",
    'quote " back \\ end\\'])
def test_toml_characters_outside_the_bmp(config_mod, command):
    # Characters outside the BMP, control characters, quotes: greetd reads what was written
    config = {"initial_session": {"command": command}}
    text = config_mod.toml_dump(config)
    assert tomllib.loads(text) == config
    assert "\\ud83d" not in text


def test_toml_writes_utf8_as_it_is(config_mod):
    text = config_mod.toml_dump({"s": {"k": "\U0001F600\u00e9"}})
    assert text == '[s]\nk = "\U0001F600\u00e9"\n'
    assert config_mod.toml_string("\x01\x7f\t") == '"\\u0001\\u007f\\t"'


def test_toml_quotes_keys_that_are_not_bare(config_mod):
    config = {"a.b": {"with space": 1, "ok_key-1": 2, "": 3, "\u00e9": 4}}
    text = config_mod.toml_dump(config)
    assert '["a.b"]' in text and '"with space" = 1' in text and "ok_key-1 = 2" in text
    assert tomllib.loads(text) == config


def test_toml_refuses_a_lone_surrogate(config_mod):
    with pytest.raises(ValueError, match="surrogate"):
        config_mod.toml_dump({"s": {"k": "a\ud83db"}})


def test_toml_file_is_utf8(config_mod, tmp_path):
    path = tmp_path / "greetd.toml"
    config_mod.write_atomic(str(path), config_mod.toml_dump({"s": {"k": "\U0001F600"}}))
    assert tomllib.loads(path.read_bytes().decode("utf-8")) == {"s": {"k": "\U0001F600"}}


# ----- plinius-greeter-config: the root side -----

ETC_TOML = """\
[terminal]
vt = 1

[default_session]
command = "agreety --cmd /bin/sh"
user = "greeter"

[initial_session]
command = "env XDG_SESSION_TYPE=wayland phosh-session --custom"
user = "alice"
"""


class Commands:
    """subprocess for plinius-greeter-config: loginctl answers from a list of sessions,
    everything else is recorded."""

    def __init__(self):
        self.calls = []
        self.inputs = []
        self.sessions = []
        self.loginctl_out = None
        # The open encryption: mapping -> (device, password); chpasswd's exit code
        self.crypt = {}
        self.chpasswd_rc = 0

    def run(self, cmd, **kw):
        self.calls.append(list(cmd))
        self.inputs.append(kw.get("input"))
        if cmd[0] == "loginctl":
            out = self.loginctl_out if self.loginctl_out is not None else json.dumps(self.sessions)
            return types.SimpleNamespace(returncode=0, stdout=out)
        if cmd[:2] == ["cryptsetup", "status"]:
            if cmd[2] not in self.crypt:
                return types.SimpleNamespace(returncode=4, stdout="", stderr="")
            return types.SimpleNamespace(returncode=0, stdout=f"/dev/mapper/{cmd[2]} is active.\n"
                                         f"  type:    LUKS2\n  device:  {self.crypt[cmd[2]][0]}\n", stderr="")
        if cmd[:2] == ["cryptsetup", "open"]:
            right = any(dev == cmd[-1] and pw == kw.get("input") for dev, pw in self.crypt.values())
            return types.SimpleNamespace(returncode=0 if right else 2, stdout="", stderr="")
        if cmd[0] == "chpasswd":
            return types.SimpleNamespace(returncode=self.chpasswd_rc, stdout="", stderr="chpasswd: PAM failure")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def root(config_mod, users, tmp_path, monkeypatch):
    """plinius-greeter-config with its paths in tmp_path, greeter = the test's own user."""
    r = types.SimpleNamespace()
    r.etc = tmp_path / "etc/greetd/config.toml"
    r.etc.parent.mkdir(parents=True)
    r.etc.write_text(ETC_TOML)
    r.run = tmp_path / "run"
    r.run_dir = r.run / "plinius-login"
    r.greeter_dir = r.run / "plinius-greeter"
    r.greeter_dir.mkdir(parents=True)
    r.request = r.greeter_dir / "request"
    r.greetd_run = r.run / "greetd.run"
    r.last = tmp_path / "var/lib/plinius-greeter/last-user"
    r.command = tmp_path / "usr/lib/plinius-greeter/plinius-greeter-session"
    r.command.parent.mkdir(parents=True)
    r.command.write_text("#!/bin/sh\n")
    r.command.chmod(0o755)
    r.shadow = tmp_path / "etc/shadow"
    r.shadow.write_text("root:!*:20400::::::\nalice:$y$j9T$salt$hash:20400:0:99999:7:::\n"
                        "bob:$6$salt$hash:20400:0:99999:7:::\n")
    r.first = r.run_dir / "first-start"
    r.password = r.greeter_dir / "password"
    r.result = r.run_dir / "password-result"
    for name, value in {"ETC_CONFIG": r.etc, "GREETD": tmp_path / "greetd", "GREETD_RUN": r.greetd_run,
                        "RUN_DIR": r.run_dir, "CONFIG": r.run_dir / "greetd.toml",
                        "NEXT_USER": r.run_dir / "next-user", "REQUEST": r.request,
                        "LAST_USER": r.last, "GREETER_COMMAND": r.command, "SHADOW": r.shadow,
                        "FIRST_START": r.first, "PASSWORD": r.password, "PASSWORD_RESULT": r.result}.items():
        monkeypatch.setattr(config_mod, name, str(value))
    r.greeter_uid = os.getuid()

    def getpwnam(name):
        if name == "greeter" and r.greeter_uid is not None:
            return entry("greeter", r.greeter_uid, "/usr/bin/nologin")
        raise KeyError(name)
    monkeypatch.setattr(config_mod, "pwd", types.SimpleNamespace(getpwnam=getpwnam))
    r.cmds = Commands()
    monkeypatch.setattr(config_mod, "subprocess", types.SimpleNamespace(run=r.cmds.run))
    users(entry("alice", 1000), entry("bob", 1001))
    r.users = users
    return r


def test_make_config_several_accounts_start_in_the_greeter(config_mod, root):
    config = config_mod.make_config()
    assert config["default_session"] == {"command": str(root.command), "user": "greeter"}
    assert "initial_session" not in config
    assert config["terminal"] == {"vt": 1}


def test_make_config_single_account_logs_in_at_once(config_mod, root):
    root.users(entry("alice", 1000))
    config = config_mod.make_config()
    assert config["initial_session"]["user"] == "alice"
    assert config["default_session"]["command"] == str(root.command)


@pytest.mark.parametrize("missing", ["user", "command"])
def test_make_config_without_the_greeter(config_mod, root, missing):
    # No greeter user or no runnable greeter: greetd's own default_session, and the automatic
    # login stays even with several accounts (there would be nothing to pick with)
    if missing == "user":
        root.greeter_uid = None
    else:
        root.command.chmod(0o644)
    config = config_mod.make_config()
    assert config["default_session"]["command"] == "agreety --cmd /bin/sh"
    assert config["initial_session"]["user"] == "alice"


def test_make_config_picked_account_with_the_configured_command(config_mod, root):
    root.run_dir.mkdir()
    (root.run_dir / "next-user").write_text("bob\n")
    config = config_mod.make_config()
    assert config["initial_session"] == {
        "command": "env XDG_SESSION_TYPE=wayland phosh-session --custom", "user": "bob"}
    # Used once
    assert not (root.run_dir / "next-user").exists()
    assert "initial_session" not in config_mod.make_config()


def test_make_config_picked_account_without_initial_session(config_mod, root):
    root.etc.write_text(ETC_TOML.split("[initial_session]")[0])
    root.run_dir.mkdir()
    (root.run_dir / "next-user").write_text("alice")
    config = config_mod.make_config()
    assert config["initial_session"] == {"command": config_mod.PHOSH_COMMAND, "user": "alice"}


def test_take_next_user_ignores_other_names(config_mod, root, capsys):
    root.run_dir.mkdir()
    for name in ("root", "", "alice\nbob", "aid_gps"):
        (root.run_dir / "next-user").write_text(name)
        assert config_mod.take_next_user() is None
        assert not (root.run_dir / "next-user").exists()
    assert "is not a login account" in capsys.readouterr().err
    assert config_mod.take_next_user() is None


class Exec(Exception):
    pass


@pytest.fixture
def execv(monkeypatch):
    calls = []

    def fake(path, argv):
        calls.append((path, argv))
        raise Exec()
    monkeypatch.setattr(os, "execv", fake)
    return calls


def test_run_greetd_writes_the_configuration_and_execs(config_mod, root, execv):
    with pytest.raises(Exec):
        config_mod.run_greetd()
    path = str(root.run_dir / "greetd.toml")
    assert execv == [(config_mod.GREETD, [config_mod.GREETD, "--config", path])]
    written = tomllib.loads((root.run_dir / "greetd.toml").read_text())
    assert written["default_session"]["user"] == "greeter"
    assert "initial_session" not in written
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    assert sorted(os.listdir(root.run_dir)) == ["greetd.toml"]


def test_run_greetd_falls_back_to_the_system_configuration(config_mod, root, execv, capsys):
    root.etc.write_text("[default_session\n")  # broken TOML
    with pytest.raises(Exec):
        config_mod.run_greetd()
    assert execv == [(config_mod.GREETD, [config_mod.GREETD, "--config", str(root.etc)])]
    assert "using" in capsys.readouterr().err
    assert not (root.run_dir / "greetd.toml").exists()


def test_run_greetd_does_not_follow_a_planted_link(config_mod, root, execv, tmp_path):
    # greetd.toml.tmp as a link (nobody but root writes there, but O_NOFOLLOW keeps it so):
    # the target is not written, greetd gets the system file
    victim = tmp_path / "victim"
    victim.write_text("keep")
    root.run_dir.mkdir()
    os.symlink(victim, root.run_dir / "greetd.toml.tmp")
    with pytest.raises(Exec):
        config_mod.run_greetd()
    assert victim.read_text() == "keep"
    assert execv[0][1][-1] == str(root.etc)


def test_user_session_on_seat(config_mod, root):
    assert config_mod.user_session_on_seat() is None
    root.cmds.loginctl_out = ""
    assert config_mod.user_session_on_seat() is None
    root.cmds.loginctl_out = None
    root.cmds.sessions = [
        {"session": "c1", "user": "greeter", "seat": "seat0", "class": "greeter"},
        {"session": "3", "user": "alice", "seat": None, "class": "user"},     # SSH
        {"session": "4", "user": "alice", "seat": "seat0", "class": "manager"},
    ]
    assert config_mod.user_session_on_seat() is None
    for cls in ("user", "user-early", "user-incomplete"):
        root.cmds.sessions[-1] = {"session": "5", "user": "bob", "seat": "seat0", "class": cls}
        assert config_mod.user_session_on_seat() == "bob"
    assert root.cmds.calls[0] == ["loginctl", "list-sessions", "--json=short"]


def systemctl_calls(root):
    return [c for c in root.cmds.calls if c[0] == "systemctl"]


def test_login_without_a_request(config_mod, root):
    assert config_mod.login() == 0
    assert root.cmds.calls == []


def test_login_starts_the_picked_account(config_mod, root, execv):
    root.request.write_text("bob\n")
    root.greetd_run.write_text("")
    assert config_mod.login() == 0
    assert not root.request.exists()
    assert (root.run_dir / "next-user").read_text() == "bob\n"
    assert root.last.read_text() == "bob\n"
    assert not root.greetd_run.exists()
    assert systemctl_calls(root) == [["systemctl", "--no-block", "restart", "greetd.service"]]
    # greetd's next start: that account as the one-time automatic login
    with pytest.raises(Exec):
        config_mod.run_greetd()
    written = tomllib.loads((root.run_dir / "greetd.toml").read_text())
    assert written["initial_session"]["user"] == "bob"
    assert not (root.run_dir / "next-user").exists()


def test_login_without_greetd_run_file(config_mod, root):
    root.request.write_text("alice")
    assert config_mod.login() == 0
    assert systemctl_calls(root)


def assert_refused(root, *, kept=()):
    assert not os.path.lexists(root.request)
    assert not (root.run_dir / "next-user").exists()
    assert not root.last.exists()
    assert systemctl_calls(root) == []
    for path in kept:
        assert os.path.lexists(path)


@pytest.mark.parametrize("name", ["root", "aid_gps", "nobody", "alice bob", "x" * 300,
                                  "../alice", "greeter"])
def test_login_refuses_names_that_are_no_login_accounts(config_mod, root, name, capsys):
    root.users(entry("alice", 1000), entry("bob", 1001), entry("root", 0),
               entry("aid_gps", 1021, "/usr/bin/nologin"), entry("greeter", 964, "/usr/bin/nologin"))
    root.request.write_text(name + "\n")
    root.greetd_run.write_text("")
    assert config_mod.login() == 1
    assert_refused(root, kept=[root.greetd_run])
    assert "not a login account" in capsys.readouterr().err


def test_login_reads_only_the_start_of_the_request(config_mod, root):
    # 256 characters at most: a name, then a lot more, is not that name
    root.request.write_text("alice" + "z" * 100000)
    assert config_mod.login() == 1
    assert_refused(root)


def test_login_refuses_a_request_of_another_user(config_mod, root, capsys):
    root.greeter_uid = os.getuid() + 1
    root.request.write_text("alice\n")
    assert config_mod.login() == 1
    assert_refused(root)
    assert "not written by greeter" in capsys.readouterr().err


def test_login_refuses_a_link(config_mod, root, tmp_path):
    # A link to a file with a login name in it, of someone else's: not read, the link goes
    target = tmp_path / "target"
    target.write_text("alice\n")
    os.symlink(target, root.request)
    assert config_mod.login() == 1
    assert_refused(root)
    assert target.read_text() == "alice\n"


def test_login_refuses_a_fifo_without_blocking(config_mod, root):
    os.mkfifo(root.request)
    assert config_mod.login() == 1
    assert_refused(root)


def test_login_refuses_a_directory(config_mod, root, capsys):
    root.request.mkdir()
    assert config_mod.login() == 1
    # Gone, or plinius-greeter-login.path (PathExists=) starts the service again and again
    assert_refused(root)
    assert "not written by greeter" in capsys.readouterr().err


def test_login_removes_a_full_directory_without_following_its_links(config_mod, root, tmp_path):
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "file").write_text("x")
    root.request.mkdir()
    (root.request / "sub").mkdir()
    (root.request / "sub/alice").write_text("alice\n")
    (root.request / "link").symlink_to(keep)
    assert config_mod.login() == 1
    assert_refused(root)
    assert (keep / "file").read_text() == "x"


def test_remove_request_when_it_is_gone_already(config_mod, root):
    # Removed meanwhile (a second start of the service): nothing to do, no error
    config_mod.remove_request()
    assert not os.path.lexists(root.request)


def test_remove_request_reports_what_it_cannot_remove(config_mod, root, monkeypatch, capsys):
    root.request.mkdir()

    def rmtree(path):
        raise PermissionError(13, "Permission denied", path)
    monkeypatch.setattr(config_mod, "shutil", types.SimpleNamespace(rmtree=rmtree))
    config_mod.remove_request()
    assert root.request.is_dir()
    assert "cannot remove" in capsys.readouterr().err


def test_login_without_greeter_user_fails_closed(config_mod, root):
    root.greeter_uid = None
    root.request.write_text("alice\n")
    with pytest.raises(KeyError):
        config_mod.login()
    assert_refused(root)


def test_login_never_ends_a_running_session(config_mod, root, capsys):
    root.cmds.sessions = [{"session": "2", "user": "alice", "seat": "seat0", "class": "user"}]
    root.request.write_text("bob\n")
    root.greetd_run.write_text("")
    assert config_mod.login() == 1
    assert_refused(root, kept=[root.greetd_run])
    assert "the session of alice is running" in capsys.readouterr().err


def test_login_ignores_greeter_and_ssh_sessions(config_mod, root):
    root.cmds.sessions = [{"session": "c1", "user": "greeter", "seat": "seat0", "class": "greeter"},
                          {"session": "7", "user": "alice", "seat": "", "class": "user"}]
    root.request.write_text("alice\n")
    assert config_mod.login() == 0
    assert root.last.read_text() == "alice\n"


@pytest.mark.parametrize("argv, code", [([], 2), (["login", "x"], 2), (["other"], 2),
                                        (["login"], 0)])
def test_config_main(config_mod, root, monkeypatch, capsys, argv, code):
    monkeypatch.setattr(sys, "argv", ["plinius-greeter-config", *argv])
    assert config_mod.main() == code
    if code == 2:
        assert "usage" in capsys.readouterr().err


def test_config_main_greetd(config_mod, root, monkeypatch, execv):
    monkeypatch.setattr(sys, "argv", ["plinius-greeter-config", "greetd"])
    with pytest.raises(Exec):
        config_mod.main()
    assert execv[0][1][1:] == ["--config", str(root.run_dir / "greetd.toml")]


# ----- plinius-greeter: module functions -----

def test_setup_locale_reads_the_system_locale(mod, tmp_path, monkeypatch):
    conf = tmp_path / "locale.conf"
    conf.write_text('LANG="de_DE.UTF-8"\nLC_TIME=en_GB.UTF-8\nLC_MESSAGES=fr_FR.UTF-8\n'
                    'FOO=bar\n# LC_ALL=x\n')
    for key in ("LANG", "LC_TIME", "FOO", "LC_ALL"):
        monkeypatch.delenv(key, raising=False)
    # Set already: stays as it is
    monkeypatch.setenv("LC_MESSAGES", "C")
    real_open = open
    opened = []

    def fake_open(path, *args, **kw):
        assert path == "/etc/locale.conf"
        opened.append(path)
        return real_open(conf, *args, **kw)
    monkeypatch.setattr(mod, "open", fake_open, raising=False)
    setlocale = fakes.recorder()
    monkeypatch.setattr(mod, "locale", fakes.Proxy(mod.locale, setlocale=setlocale))
    text = fakes.recorder()
    monkeypatch.setattr(mod, "gettext", types.SimpleNamespace(bindtextdomain=text, textdomain=text))
    mod.setup_locale()
    assert opened == ["/etc/locale.conf"]
    assert os.environ["LANG"] == "de_DE.UTF-8" and os.environ["LC_TIME"] == "en_GB.UTF-8"
    assert os.environ["LC_MESSAGES"] == "C"
    assert "FOO" not in os.environ and "LC_ALL" not in os.environ
    assert setlocale.calls == [(mod.locale.LC_ALL, "")]
    assert text.calls == [("plinius-greeter", "/usr/share/locale"), ("plinius-greeter",)]


def test_setup_locale_keeps_lang_and_falls_back_to_c(mod, monkeypatch):
    import locale
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setattr(mod, "open", lambda *a, **k: pytest.fail("locale.conf read"), raising=False)
    calls = []

    def setlocale(category, name):
        calls.append(name)
        if name == "":
            raise locale.Error("unsupported locale setting")
    monkeypatch.setattr(mod, "locale", fakes.Proxy(mod.locale, setlocale=setlocale))
    monkeypatch.setattr(mod, "gettext", types.SimpleNamespace(bindtextdomain=fakes.recorder(),
                                                              textdomain=fakes.recorder()))
    mod.setup_locale()
    assert calls == ["", "C.UTF-8"]


def test_setup_locale_without_locale_conf(mod, monkeypatch):
    monkeypatch.delenv("LANG", raising=False)

    def missing(*_a, **_k):
        raise FileNotFoundError("/etc/locale.conf")
    monkeypatch.setattr(mod, "open", missing, raising=False)
    monkeypatch.setattr(mod, "locale", fakes.Proxy(mod.locale, setlocale=fakes.recorder()))
    monkeypatch.setattr(mod, "gettext", types.SimpleNamespace(bindtextdomain=fakes.recorder(),
                                                              textdomain=fakes.recorder()))
    mod.setup_locale()
    assert "LANG" not in os.environ


def test_read_last_user(mod, tmp_path, monkeypatch):
    last = tmp_path / "last-user"
    monkeypatch.setattr(mod, "LAST_USER", str(last))
    monkeypatch.setattr(mod, "TEST", False)
    assert mod.read_last_user() == ""
    last.write_text(" bob \n")
    assert mod.read_last_user() == "bob"
    monkeypatch.setattr(mod, "TEST", True)
    monkeypatch.setenv("PLINIUS_GREETER_LAST", "carol")
    assert mod.read_last_user() == "carol"
    monkeypatch.delenv("PLINIUS_GREETER_LAST")
    assert mod.read_last_user() == ""


# ----- the first start: an account without a password -----

def without_password(root, name="alice", field="!*"):
    text = root.shadow.read_text()
    lines = [line if not line.startswith(name + ":") else f"{name}:{field}:20400:0:99999:7:::"
             for line in text.splitlines()]
    root.shadow.write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize("field, empty", [("!*", True), ("!", True), ("!!", True), ("*", True), ("", False),
                                          ("!$y$j9T$salt$hash", False), ("$6$salt$hash", False)])
def test_needs_password(config_mod, root, field, empty):
    without_password(root, field=field)
    assert config_mod.needs_password("alice") is empty


def test_needs_password_unknown_account_or_unreadable(config_mod, root, capsys):
    without_password(root)
    assert config_mod.needs_password("carol") is False
    root.shadow.unlink()
    assert config_mod.needs_password("alice") is False
    assert "cannot read" in capsys.readouterr().err


def test_make_config_account_without_password_starts_in_the_greeter(config_mod, root):
    root.users(entry("alice", 1000))
    without_password(root)
    root.run_dir.mkdir()
    config = config_mod.make_config()
    assert "initial_session" not in config
    assert root.first.read_text() == "alice\n"
    # Once it has one, the automatic login is back and the note gone
    without_password(root, field="$y$j9T$salt$hash")
    assert config_mod.make_config()["initial_session"]["user"] == "alice"
    assert not root.first.exists()
    config_mod.note_first_start([])


def test_make_config_first_start_needs_the_greeter(config_mod, root):
    root.users(entry("alice", 1000))
    without_password(root)
    root.greeter_uid = None
    assert config_mod.make_config()["initial_session"]["user"] == "alice"
    assert not root.first.exists()


def test_make_config_picked_account_beats_the_first_start(config_mod, root):
    without_password(root, "bob")
    root.run_dir.mkdir()
    (root.run_dir / "next-user").write_text("alice\n")
    assert config_mod.make_config()["initial_session"]["user"] == "alice"
    assert root.first.read_text() == "bob\n"


def test_run_greetd_drops_an_old_password_result(config_mod, root, execv):
    root.run_dir.mkdir()
    root.result.write_text("same\n")
    with pytest.raises(Exec):
        config_mod.run_greetd()
    assert not root.result.exists()


def ask(root, user="alice", secret="2580"):
    root.password.write_text(json.dumps({"user": user, "secret": secret}))


def chpasswd_calls(root):
    return [(c, i) for c, i in zip(root.cmds.calls, root.cmds.inputs) if c[0] == "chpasswd"]


def test_password_without_a_request(config_mod, root):
    assert config_mod.password() == 0
    assert root.cmds.calls == []


def test_password_sets_the_first_pin_and_logs_in(config_mod, root):
    without_password(root)
    root.run_dir.mkdir()
    root.first.write_text("alice\n")
    ask(root)
    assert config_mod.password() == 0
    assert not root.password.exists()
    assert chpasswd_calls(root) == [(["chpasswd"], "alice:2580\n")]
    assert root.result.read_text() == "ok\n"
    assert not root.first.exists()
    assert (root.run_dir / "next-user").read_text() == "alice\n"
    assert systemctl_calls(root) == [["systemctl", "--no-block", "restart", "greetd.service"]]


def test_password_a_password_with_letters(config_mod, root):
    without_password(root)
    ask(root, secret="Seven Seas 7")
    assert config_mod.password() == 0
    assert chpasswd_calls(root) == [(["chpasswd"], "alice:Seven Seas 7\n")]


def test_password_never_replaces_one_that_is_set(config_mod, root, capsys):
    ask(root)
    assert config_mod.password() == 1
    assert chpasswd_calls(root) == [] and root.result.read_text() == "invalid\n"
    assert not root.password.exists()
    assert "no login account without a password" in capsys.readouterr().err


@pytest.mark.parametrize("text", ["not json", "[]", '{"user": "alice"}', '{"user": 1, "secret": "2580"}',
                                  '{"user": "root", "secret": "2580"}'])
def test_password_refuses_what_is_no_request(config_mod, root, text):
    without_password(root)
    without_password(root, "root")
    root.password.write_text(text)
    assert config_mod.password() == 1
    assert chpasswd_calls(root) == [] and root.result.read_text() == "invalid\n"


@pytest.mark.parametrize("secret, problem", [("1234", ""), ("1" * 12, ""), ("123", "invalid"), ("1" * 13, "invalid"),
                                             ("abc12", "invalid"), ("abc123", ""), ("abc\n123", "invalid"),
                                             ("x" * 129, "invalid"), ("Grüße 2026", "")])
def test_secret_problem(config_mod, secret, problem):
    assert config_mod.secret_problem(secret) == problem


def test_password_refuses_a_bad_secret(config_mod, root):
    without_password(root)
    ask(root, secret="12")
    assert config_mod.password() == 1
    assert root.result.read_text() == "invalid\n" and chpasswd_calls(root) == []


@pytest.mark.parametrize("mapping", ["plinius-data", "plinius-root"])
def test_password_never_the_one_of_the_encryption(config_mod, root, mapping, capsys):
    without_password(root)
    root.cmds.crypt = {mapping: ("/dev/sda32", "2580")}
    ask(root)
    assert config_mod.password() == 1
    assert root.result.read_text() == "same\n" and chpasswd_calls(root) == []
    assert "refusing the password of the encryption" in capsys.readouterr().err
    # Another one goes through; the check is only a test of the passphrase
    ask(root, secret="1357")
    assert config_mod.password() == 0
    opens = [c for c in root.cmds.calls if c[:2] == ["cryptsetup", "open"]]
    assert all(c == ["cryptsetup", "open", "--test-passphrase", "--key-file", "-", "/dev/sda32"] for c in opens)


def test_password_chpasswd_fails(config_mod, root, capsys):
    without_password(root)
    root.cmds.chpasswd_rc = 1
    ask(root)
    assert config_mod.password() == 1
    assert root.result.read_text() == "failed\n"
    assert "PAM failure" in capsys.readouterr().err
    assert systemctl_calls(root) == []


def test_password_request_of_another_user(config_mod, root):
    without_password(root)
    ask(root)
    root.greeter_uid = os.getuid() + 1
    assert config_mod.password() == 1
    assert not root.password.exists() and chpasswd_calls(root) == []


def test_main_password(config_mod, root, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["plinius-greeter-config", "password"])
    assert config_mod.main() == 0


def test_base_accounts_from_the_login_accounts(mod, tmp_path, monkeypatch):
    icons = tmp_path / "icons"
    icons.mkdir()
    (icons / "bob").write_bytes(b"png")
    (icons / "alice").mkdir()  # not a file
    monkeypatch.setattr(mod, "ICON_DIR", str(icons))
    monkeypatch.setattr(mod, "TEST", False)
    monkeypatch.setattr(mod.accounts, "login_users",
                        lambda: [entry("alice", 1000, gecos="Alice A"), entry("bob", 1001)])
    found = mod.base_accounts()
    assert [(a.name, a.icon) for a in found] == [("alice", None), ("bob", str(icons / "bob"))]


def test_base_accounts_for_screenshots(mod, monkeypatch):
    monkeypatch.setattr(mod, "TEST", True)
    monkeypatch.setenv("PLINIUS_GREETER_USERS", "anna:Anna A, ben ,carl")
    monkeypatch.setattr(mod.accounts, "login_users", lambda: pytest.fail("real accounts read"))
    assert [(a.name, a.icon) for a in mod.base_accounts()] == [
        ("anna", None), ("ben", None), ("carl", None)]
    # TEST without a list: the real rule
    monkeypatch.delenv("PLINIUS_GREETER_USERS")
    monkeypatch.setattr(mod.accounts, "login_users", lambda: [])
    assert mod.base_accounts() == []


def test_ordered(mod):
    names = ["carol", "alice", "bob"]
    accts = [mod.Account(n) for n in names]
    assert [a.name for a in mod.ordered(accts, "bob")] == ["bob", "alice", "carol"]
    assert [a.name for a in mod.ordered(accts, "")] == ["alice", "bob", "carol"]
    assert [a.name for a in mod.ordered(accts, "nobody")] == ["alice", "bob", "carol"]


@pytest.mark.parametrize("t_fmt, expected", [
    ("%I:%M:%S %p", "%-l:%M"), ("%r", "%-l:%M"), ("%l:%M %p", "%-l:%M"),
    ("%H:%M:%S", "%H:%M"), ("%T", "%H:%M"), (AttributeError, "%H:%M"), (ValueError, "%H:%M")])
def test_time_format(mod, monkeypatch, t_fmt, expected):
    def langinfo(_item):
        if isinstance(t_fmt, type):
            raise t_fmt()
        return t_fmt
    monkeypatch.setattr(mod, "locale", fakes.Proxy(mod.locale, nl_langinfo=langinfo))
    assert mod.time_format() == expected


# ----- plinius-greeter: the window -----

class Widget:
    """A GTK widget as far as the greeter uses one: children, CSS classes, state, signals."""

    made = []

    def __init__(self, *args, **kw):
        self.props = kw
        self.children = []
        self.css = set(kw.get("css_classes") or [])
        self.visible = kw.get("visible", True)
        self.can_target = kw.get("can_target", True)
        self.label = kw.get("label")
        self.sensitive = kw.get("sensitive", True)
        self.opacity = 1
        self.handlers = {}
        self.controllers = []
        self.image = None
        Widget.made.append(self)

    def append(self, child):
        self.children.append(child)

    def remove(self, child):
        self.children.remove(child)

    def get_first_child(self):
        return self.children[0] if self.children else None

    def add_css_class(self, name):
        self.css.add(name)

    def remove_css_class(self, name):
        self.css.discard(name)

    def set_visible(self, value):
        self.visible = value

    def get_visible(self):
        return self.visible

    def set_can_target(self, value):
        self.can_target = value

    def set_label(self, text):
        self.label = text

    def set_opacity(self, value):
        self.opacity = value

    def set_sensitive(self, value):
        self.sensitive = value

    def set_custom_image(self, texture):
        self.image = texture

    def add_controller(self, controller):
        self.controllers.append(controller)

    def connect(self, signal, handler):
        self.handlers.setdefault(signal, []).append(handler)

    def emit(self, signal, *args):
        for handler in self.handlers.get(signal, []):
            handler(self, *args)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


class Entry(Widget):
    """Gtk.PasswordEntry and Gtk.ToggleButton: text and state."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.text = ""
        self.active = kw.get("active", False)
        self.focused = False
        self.sensitive = True

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text

    def get_active(self):
        return self.active

    def grab_focus(self):
        self.focused = True

    def set_sensitive(self, value):
        self.sensitive = value


class FakeLfb:
    """libfeedback: init, and the events triggered (each finished at once)."""

    def __init__(self, inited=True, fail=None):
        self.inited, self.fail, self.events, self.init_ids = inited, fail, [], []
        lfb = self

        class Event:
            def __init__(self, name):
                self.name = name

            @classmethod
            def new(cls, name):
                return cls(name)

            def trigger_feedback_async(self, cancellable, callback):
                lfb.events.append(self.name)
                callback(self, "result")

            def trigger_feedback_finish(self, result):
                if lfb.fail:
                    raise GLib.Error(lfb.fail)
                return True
        self.Event = Event

    def init(self, app_id):
        self.init_ids.append(app_id)
        if self.fail:
            raise GLib.Error(self.fail)
        self.inited = True

    def is_initted(self):
        return self.inited


class Dust(Widget):
    """DustArea: whether its effect runs."""

    def __init__(self, density="medium"):
        super().__init__()
        self.density = density
        self.running = False

    def set_running(self, running):
        self.running = running


class Texture:
    loaded = []

    @classmethod
    def new_from_filename(cls, path):
        if not os.path.isfile(path) or open(path, "rb").read() != b"png":
            raise GLib.Error("not an image")
        cls.loaded.append(path)
        return ("texture", path)


class Bus:
    def __init__(self):
        self.calls = []
        self.fail = False

    def call(self, name, path, iface, method, params, *rest):
        self.calls.append((name, path, iface, method, params.unpack()))

    def call_sync(self, name, path, iface, method, params, *rest):
        if self.fail:
            raise GLib.Error("Access denied")
        self.calls.append((name, path, iface, method, params.unpack()))


@pytest.fixture
def ui(mod, tmp_path, monkeypatch):
    """Makes Greeter windows with faked widgets, clock and bus; accounts and paths in tmp_path."""
    glib = fakes.FakeGLib()
    monkeypatch.setattr(mod, "GLib", glib)
    for name in ("Overlay", "Label", "ScrolledWindow", "Button", "GestureClick", "Grid",
                 "EventControllerLegacy", "EventControllerKey", "Picture", "DrawingArea", "GestureLongPress"):
        monkeypatch.setattr(mod.Gtk, name, Widget)
    monkeypatch.setattr(mod, "DustArea", Dust)
    lfb = FakeLfb()
    monkeypatch.setattr(mod, "Lfb", lfb)
    look = mod.Look()
    monkeypatch.setattr(mod, "installation_look", lambda: look)
    for name in ("Box", "PasswordEntry", "ToggleButton"):
        monkeypatch.setattr(mod.Gtk, name, Entry)
    monkeypatch.setattr(mod.Adw, "Spinner", Widget)
    monkeypatch.setattr(mod.Adw, "Avatar", Widget)
    monkeypatch.setattr(mod.Gdk, "Texture", Texture)
    monkeypatch.setattr(mod.Gdk, "KEY_PowerOff", POWER_KEY)
    monkeypatch.setattr(mod.Gdk, "KEY_Escape", ESCAPE_KEY)
    monkeypatch.setattr(Widget, "made", [])
    monkeypatch.setattr(Texture, "loaded", [])
    monkeypatch.setattr(mod, "TEST", False)
    monkeypatch.setattr(mod, "AccountsService", None)
    run = tmp_path / "run"
    run.mkdir()
    login = tmp_path / "login"
    login.mkdir()
    monkeypatch.setattr(mod, "REQUEST_DIR", str(run))
    monkeypatch.setattr(mod, "FIRST_START", str(login / "first-start"))
    monkeypatch.setattr(mod, "PASSWORD_RESULT", str(login / "password-result"))
    monkeypatch.setattr(mod, "LAST_USER", str(tmp_path / "last-user"))
    monkeypatch.setattr(mod, "ICON_DIR", str(tmp_path / "icons"))
    bus = Bus()
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(Gio, bus_get_sync=lambda _t: bus))
    light = []

    def set_backlight(value):
        light.append(value)
        return 120
    monkeypatch.setattr(mod, "set_backlight", set_backlight)
    current = []
    monkeypatch.setattr(mod.accounts, "login_users", lambda: list(current))

    def make(*names, last=None, first=None):
        current[:] = [entry(n, 1000 + i) for i, n in enumerate(names)]
        if last is not None:
            (tmp_path / "last-user").write_text(last + "\n")
        if first is not None:
            (login / "first-start").write_text(first + "\n")
        return mod.Greeter(None)

    return types.SimpleNamespace(make=make, glib=glib, run=run, bus=bus, light=light, look=look, lfb=lfb,
                                 request=run / "request", tmp=tmp_path, login=login,
                                 password=run / "password", result=login / "password-result")


def rows(g):
    return [[tile.props["child"].children[1].label for tile in row.children]
            for row in g.rows.children]


def sources(glib, kind, interval=None):
    return [sid for sid, s in glib.sources.items()
            if s[0] == kind and (interval is None or s[1] == interval)]


def idle_timer(mod, glib):
    found = [sid for sid, s in glib.sources.items()
             if s[0] == "seconds" and s[1] == mod.IDLE_SECONDS]
    assert len(found) <= 1
    return found[0] if found else None


def test_tiles_in_rows_of_two_last_user_first(mod, ui):
    g = ui.make("carol", "alice", "bob", last="bob")
    assert rows(g) == [["bob", "alice"], ["carol"]]
    assert "last" in g.tiles["bob"].css
    assert "last" not in g.tiles["alice"].css
    assert g.title.visible and g.subtitle.visible
    # Nothing happens by itself with a choice to make
    assert sources(ui.glib, "idle") == []
    assert not ui.request.exists()


def test_no_accounts_shows_an_empty_choice(mod, ui):
    g = ui.make()
    assert rows(g) == [] and g.title.visible
    assert sources(ui.glib, "idle") == []


def test_a_single_account_is_logged_in_by_itself(mod, ui):
    g = ui.make("alice", last="alice")
    assert rows(g) == []
    assert not g.title.visible and not g.subtitle.visible
    (sid,) = sources(ui.glib, "idle")
    ui.glib.fire(sid)
    assert ui.request.read_text() == "alice\n"
    assert g.choosing.name == "alice"
    assert g.status_label.label == "Starting…" and g.spinner.visible and g.status.opacity == 1
    assert sources(ui.glib, "seconds", mod.START_TIMEOUT_SECONDS)


def test_tap_hands_the_account_over(mod, ui):
    g = ui.make("alice", "bob")
    g.tiles["bob"].emit("clicked")
    assert ui.request.read_text() == "bob\n"
    assert stat.S_IMODE(ui.request.stat().st_mode) == 0o600
    assert not (ui.run / "request.tmp").exists()
    assert "chosen" in g.tiles["bob"].css and "dimmed" in g.tiles["alice"].css
    assert not any(t.can_target for t in g.tiles.values())
    # A second tap (or a late AccountsService) changes nothing while it starts
    g.tiles["alice"].emit("clicked")
    g.fill()
    assert ui.request.read_text() == "bob\n"
    assert "chosen" in g.tiles["bob"].css


def test_start_timeout_offers_the_accounts_again(mod, ui):
    g = ui.make("alice")
    ui.glib.fire(sources(ui.glib, "idle")[0])
    ui.request.unlink()  # root took it
    (sid,) = sources(ui.glib, "seconds", mod.START_TIMEOUT_SECONDS)
    ui.glib.fire(sid)
    assert g.choosing is None and g.failed and g.start_timeout == 0
    assert "error" in g.status_label.css and not g.spinner.visible
    assert "could not start" in g.status_label.label
    # After a failed start the single account is a tile to try again by hand, not again by itself
    assert rows(g) == [["alice"]] and g.title.visible
    assert sources(ui.glib, "idle") == []
    assert g.tiles["alice"].can_target and "dimmed" not in g.tiles["alice"].css
    g.tiles["alice"].emit("clicked")
    assert ui.request.read_text() == "alice\n"
    assert "error" not in g.status_label.css


def test_request_does_not_follow_a_planted_link(mod, ui, capsys):
    victim = ui.tmp / "victim"
    victim.write_text("keep\n")
    os.symlink(victim, ui.run / "request.tmp")
    g = ui.make("alice", "bob")
    g.tiles["alice"].emit("clicked")
    assert victim.read_text() == "keep\n"
    assert not ui.request.exists()
    assert g.choosing is None and "error" in g.status_label.css
    assert "cannot hand over alice" in capsys.readouterr().err
    assert sources(ui.glib, "seconds", mod.START_TIMEOUT_SECONDS) == []


def test_request_directory_missing(mod, ui):
    ui.run.rmdir()
    g = ui.make("alice", "bob")
    g.tiles["bob"].emit("clicked")
    assert g.failed and "error" in g.status_label.css


def test_test_mode_writes_no_request(mod, ui, monkeypatch, capsys):
    monkeypatch.setattr(mod, "TEST", True)
    g = ui.make("alice", "bob")
    g.tiles["alice"].emit("clicked")
    assert "plinius-greeter: request alice" in capsys.readouterr().out
    assert not ui.request.exists()


# ----- the first start in the window -----

def keys(g, text):
    for k in text:
        g.keys[{"<": "back", ">": "next"}.get(k, k)].emit("clicked")


def answer(ui, result):
    """Root's answer, newer than what the greeter saw before it asked."""
    ui.result.write_text(result + "\n")
    os.utime(ui.result, ns=(time_ns := os.stat(ui.result).st_mtime_ns + 10**9, time_ns))
    (sid,) = sources(ui.glib, "timeout", 500)
    return ui.glib.fire(sid)


def test_first_start_asks_instead_of_logging_in(mod, ui):
    g = ui.make("alice", first="alice")
    assert g.first == "alice" and g.first_box.visible
    assert rows(g) == [] and sources(ui.glib, "idle") == []
    assert g.title.label == "Protect your phone" and g.title.visible
    assert g.pin_box.visible and not g.password_box.visible
    assert g.first_prompt.label == "Choose a PIN of 4 to 12 digits."
    # Without the note the accounts are back
    (ui.login / "first-start").unlink()
    assert not ui.make("alice", "bob").first_box.visible


def test_first_start_in_the_installations_look(mod, ui):
    # No clock: the installation's background and dust, as the phone will look after it
    g = ui.make("alice", first="alice")
    assert not g.clock.visible and not g.date.visible
    assert g.installation_bg.visible and g.installation_bg.children == []
    assert g.dust.visible and g.dust.running and g.dust.density == "medium"
    assert not g.installation_bg.can_target
    # The account picker keeps its clock and its own background, the dust stands still
    (ui.login / "first-start").unlink()
    g = ui.make("alice", "bob")
    assert g.clock.visible and g.date.visible
    assert not g.installation_bg.visible and not g.dust.visible and not g.dust.running


def test_first_start_as_phoshs_unlock_page(mod, ui):
    # Keyboard bottom left, delete bottom right, Continue below the keypad, off until a digit
    g = ui.make("alice", first="alice")
    assert set(g.keys) == set("0123456789") | {"password", "back", "next"}
    assert g.keys["password"].props["icon_name"] == "input-keyboard-symbolic"
    assert g.keys["back"].props["icon_name"] == "edit-clear-symbolic"
    assert g.keys["next"] is g.pin_next and not g.pin_next.sensitive
    keys(g, "25")
    assert g.pin_next.sensitive and g.dots.label == "●●"
    keys(g, "<")
    assert g.dots.label == "●"
    # A long press on delete clears the PIN
    (hold,) = g.keys["back"].controllers
    keys(g, "80")
    hold.emit("pressed", 1.0, 1.0)
    assert g.digits == "" and g.dots.label == "" and not g.pin_next.sensitive


def test_taps_vibrate(mod, ui):
    # Each key of the keypad, Continue, back to the PIN, the password's Continue
    g = ui.make("alice", first="alice")
    keys(g, "12<")
    (hold,) = g.keys["back"].controllers
    hold.emit("pressed", 1.0, 1.0)
    assert ui.lfb.events == ["button-pressed"] * 4
    keys(g, ">")
    g.keys["password"].emit("clicked")
    g.pin_mode.emit("clicked")
    g.set_kind("password")
    g.password_next.emit("clicked")
    assert len(ui.lfb.events) == 8 and set(ui.lfb.events) == {"button-pressed"}
    # Not while root sets the password
    g.password_timeout = 7
    keys(g, "5")
    g.submit_password()
    assert len(ui.lfb.events) == 8


def test_taps_vibrate_on_tiles_and_in_the_power_menu(mod, ui):
    g = ui.make("alice", "bob")
    open_power_menu(mod, ui, g)
    assert ui.lfb.events == ["button-pressed"]
    (close,) = [w for w in Widget.made if "power-close" in w.css]
    close.emit("clicked")
    open_power_menu(mod, ui, g)
    g.power_entries["Reboot"].emit("clicked")
    g.tiles["alice"].emit("clicked")
    assert ui.lfb.events == ["button-pressed"] * 5


def test_no_vibration_without_feedbackd(mod, ui, monkeypatch, capsys):
    g = ui.make("alice", first="alice")
    ui.lfb.inited = False
    keys(g, "1")
    assert ui.lfb.events == []
    monkeypatch.setattr(mod, "Lfb", None)
    keys(g, "2")
    mod.setup_feedback()
    assert g.digits == "12"
    # feedbackd answering with an error: the tap goes on, the error is logged
    lfb = FakeLfb(fail="no vibra")
    monkeypatch.setattr(mod, "Lfb", lfb)
    mod.setup_feedback()
    assert lfb.init_ids == ["org.plinius.Greeter"] and "no vibra" in capsys.readouterr().err
    lfb.inited = True
    keys(g, "3")
    assert g.digits == "123" and lfb.events == ["button-pressed"]
    assert "feedback: no vibra" in capsys.readouterr().err


def test_setup_feedback(mod, monkeypatch):
    lfb = FakeLfb(inited=False)
    monkeypatch.setattr(mod, "Lfb", lfb)
    mod.setup_feedback()
    assert lfb.init_ids == ["org.plinius.Greeter"] and lfb.is_initted()


def test_keys_take_no_focus_and_stay_round(mod, ui):
    # No key stays marked after a tap or at the start, and each keeps its own square in the grid
    g = ui.make("alice", first="alice")
    for key in g.keys.values():
        if key is g.pin_next:
            continue
        assert key.props["focusable"] is False and key.props["focus_on_click"] is False
        assert key.props["halign"] == key.props["valign"] == mod.Gtk.Align.CENTER
    css = mod.make_css(mod.Look())
    assert "button.key:hover" not in css and "button.key:focus" not in css and "button.key:active {" in css


def test_less_room_above_on_the_first_start(mod, ui, monkeypatch):
    margins = {}
    monkeypatch.setattr(Widget, "set_margin_top", lambda self, v: margins.__setitem__((id(self), "top"), v),
                        raising=False)
    monkeypatch.setattr(Widget, "set_margin_bottom", lambda self, v: margins.__setitem__((id(self), "bottom"), v),
                        raising=False)
    g = ui.make("alice", first="alice")
    assert margins[id(g.column), "top"] == mod.COLUMN_TOP_FIRST and margins[id(g.subtitle), "bottom"] == 12
    (ui.login / "first-start").unlink()
    g = ui.make("alice", "bob")
    assert margins[id(g.column), "top"] == mod.COLUMN_TOP and margins[id(g.subtitle), "bottom"] == 24


def test_keypad_as_big_as_on_the_lock_screen(mod):
    # The phone: 476 x 1058 logical pixels; small and big screens stop at 80 and 120
    assert mod.keypad_button_size(476, 1058) == 103
    assert mod.keypad_button_size(360, 640) == 80
    assert mod.keypad_button_size(1200, 2000) == 120
    assert mod.keypad_css(103) == ("button.key { min-width: 103px; min-height: 103px; }\n"
                                   "button.key label { font-size: 41px; }\n")


def test_keypad_follows_the_screen(mod, ui, monkeypatch):
    loaded = []
    added = []

    class Provider:
        def load_from_string(self, css):
            loaded.append(css)
    monkeypatch.setattr(mod.Gtk, "CssProvider", Provider)
    monkeypatch.setattr(mod.Gtk, "StyleContext", types.SimpleNamespace(
        add_provider_for_display=lambda display, provider, priority: added.append((provider, priority))))
    monkeypatch.setattr(mod.Gtk, "STYLE_PROVIDER_PRIORITY_APPLICATION", 600)
    g = ui.make("alice", first="alice")
    (sizer,) = [w for w in Widget.made if "resize" in w.handlers]
    sizer.emit("resize", 0, 0)  # not laid out yet
    assert loaded == [] and g.keypad_css is None
    sizer.emit("resize", 476, 1058)
    sizer.emit("resize", 360, 640)
    assert loaded == [mod.keypad_css(103), mod.keypad_css(80)] and added == [(g.keypad_css, 601)]


def test_first_start_with_a_picture_and_without_dust(mod, ui, tmp_path):
    picture = tmp_path / "bg.png"
    picture.write_bytes(b"png")
    ui.look.picture, ui.look.effect, ui.look.density = str(picture), "snow", "low"
    g = ui.make("alice", first="alice")
    (shown,) = g.installation_bg.children
    assert shown.props["file"].get_path() == str(picture)
    # Only the dust is drawn here; another effect leaves the background alone
    assert g.dust is None
    g.blank_screen()
    g.wake()
    g.fail()


def test_dust_rests_while_the_screen_is_dark(mod, ui):
    g = ui.make("alice", first="alice")
    g.blank_screen()
    assert not g.dust.running
    g.wake()
    assert g.dust.running


def test_dust_stops_when_the_first_start_is_over(mod, ui):
    # The password is set but the account did not start: the tiles come back without the dust
    g = ui.make("alice", "bob", first="alice")
    (ui.login / "first-start").unlink()
    g.fail()
    assert not g.dust.running and not g.dust.visible and g.clock.visible and not g.first_box.visible
    g.blank_screen()
    g.wake()
    assert not g.dust.running


def test_first_pin_typed_twice_goes_to_root(mod, ui):
    g = ui.make("alice", first="alice")
    keys(g, "25809<")
    assert g.dots.label == "●●●●" and g.digits == "2580"
    keys(g, ">")
    assert g.first_prompt.label == "Repeat the PIN." and g.dots.label == ""
    keys(g, "2580>")
    assert json.loads(ui.password.read_text()) == {"user": "alice", "secret": "2580"}
    assert stat.S_IMODE(ui.password.stat().st_mode) == 0o600
    assert not g.first_box.sensitive and g.spinner.visible and g.status_label.label == "Setting it up…"
    # More keys while root works change nothing
    keys(g, "1")
    assert g.digits == "2580"
    # Nothing new yet: it waits
    (sid,) = sources(ui.glib, "timeout", 500)
    assert ui.glib.fire(sid) is not False
    assert answer(ui, "ok") is False
    assert g.choosing.name == "alice" and g.status_label.label == "Starting…"
    assert sources(ui.glib, "seconds", mod.START_TIMEOUT_SECONDS)


def test_first_pin_too_short_or_not_the_same(mod, ui):
    g = ui.make("alice", first="alice")
    keys(g, "123>")
    assert "4 to 12 digits" in g.status_label.label and "error" in g.status_label.css
    assert g.first_pin is None
    keys(g, "<<<" + "1" * 14)
    assert g.digits == "1" * 12
    keys(g, ">" + "1" * 11 + ">")
    assert "do not match" in g.status_label.label
    assert g.first_pin is None and g.digits == "" and not ui.password.exists()
    keys(g, "1234>")
    assert g.status.opacity == 0


@pytest.mark.parametrize("result, text", [("same", "password of the encryption"), ("invalid", "cannot be used"),
                                          ("failed", "could not be set"), ("", "could not be set")])
def test_first_pin_refused_by_root(mod, ui, result, text):
    g = ui.make("alice", first="alice")
    ui.result.write_text("old\n")
    keys(g, "2580>2580>")
    assert answer(ui, result) is False
    assert text in g.status_label.label and "error" in g.status_label.css
    assert g.first_box.sensitive and g.digits == "" and g.first_pin is None
    assert g.password_timeout == 0 and g.choosing is None


def test_first_pin_without_an_answer(mod, ui):
    g = ui.make("alice", first="alice")
    keys(g, "2580>2580>")
    (sid,) = sources(ui.glib, "timeout", 500)
    for _ in range(mod.PASSWORD_TIMEOUT_SECONDS * 2 - 1):
        assert ui.glib.fire(sid) is not False
    assert ui.glib.fire(sid) is False
    assert "could not be set" in g.status_label.label


def test_first_pin_cannot_be_handed_over(mod, ui, capsys):
    g = ui.make("alice", first="alice")
    ui.run.rmdir()
    keys(g, "2580>2580>")
    assert "could not be set" in g.status_label.label
    assert "cannot hand over the password" in capsys.readouterr().err


def test_first_password_with_the_keyboard(mod, ui, monkeypatch):
    started = []
    monkeypatch.setattr(mod.os, "access", lambda path, mode: path == mod.KEYBOARD)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **kw: started.append((argv, kw)) or "proc")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(ui.tmp))
    g = ui.make("alice", first="alice")
    # The keyboard bottom left of the keypad, as on the lock screen
    g.keys["password"].emit("clicked")
    assert g.kind == "password" and g.password_box.visible and not g.pin_box.visible and g.secret.focused
    ((argv, kw),) = started
    assert argv == [mod.KEYBOARD] and kw["env"]["GSETTINGS_BACKEND"] == "keyfile"
    keyfile = ui.tmp / "plinius-greeter-keyboard/glib-2.0/settings/keyfile"
    assert "screen-keyboard-enabled=true" in keyfile.read_text()
    g.set_kind("password")
    g.start_keyboard()
    assert len(started) == 1
    for secret, again, text in (("short", "short", "at least 6"), ("123456", "123456", "choose PIN"),
                                ("Seven Seas", "Seven Sea", "do not match")):
        g.secret.text, g.again.text = secret, again
        g.password_next.emit("clicked")
        assert text in g.status_label.label
    g.secret.text = g.again.text = "Seven Seas"
    g.again.emit("activate")
    assert json.loads(ui.password.read_text())["secret"] == "Seven Seas"
    g.submit_password()  # while root works
    g.secret.emit("activate")
    answer(ui, "same")
    assert g.secret.text == "" and g.again.text == ""
    # Back to the PIN with the keypad button beside Continue
    g.pin_mode.emit("clicked")
    assert g.kind == "pin" and g.pin_box.visible and not g.password_box.visible


def test_first_keyboard_missing_or_failing(mod, ui, monkeypatch, capsys):
    g = ui.make("alice", first="alice")
    monkeypatch.setattr(mod.os, "access", lambda path, mode: False)
    g.start_keyboard()
    assert g.keyboard is None
    monkeypatch.setattr(mod.os, "access", lambda path, mode: True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(ui.tmp / "missing" / "deeper"))

    def refuse(*_a, **_k):
        raise OSError("no such program")
    monkeypatch.setattr(mod.subprocess, "Popen", refuse)
    g.start_keyboard()
    assert g.keyboard is None and "on-screen keyboard" in capsys.readouterr().err


def test_first_start_test_mode(mod, ui, monkeypatch, capsys):
    monkeypatch.setattr(mod, "TEST", True)
    monkeypatch.setenv("PLINIUS_GREETER_FIRST", "alice")
    g = ui.make("alice")
    assert g.first == "alice"
    g.start_keyboard()
    assert g.keyboard is None
    keys(g, "2580>2580>")
    assert "first pin for alice" in capsys.readouterr().out
    assert not ui.password.exists()


def test_first_start_failed_login_shows_the_account(mod, ui):
    g = ui.make("alice", first="alice")
    keys(g, "2580>2580>")
    answer(ui, "ok")
    (ui.login / "first-start").unlink()  # root set the password
    (sid,) = sources(ui.glib, "seconds", mod.START_TIMEOUT_SECONDS)
    ui.glib.fire(sid)
    assert not g.first_box.visible and g.title.label == "Who is using the phone?"
    assert rows(g) == [["alice"]]


def test_secret_problem_in_the_window(mod):
    assert mod.secret_problem("pin", "2580", "2580") == ""
    assert "4 to 12" in mod.secret_problem("pin", "٢٥٨٠", "٢٥٨٠")
    assert mod.secret_problem("password", "Grüße 2026", "Grüße 2026") == ""
    assert "at least 6" in mod.secret_problem("password", "ab\ncdef", "ab\ncdef")


def test_tile_picture(mod, ui):
    icons = ui.tmp / "icons"
    icons.mkdir()
    (icons / "alice").write_bytes(b"png")
    (icons / "bob").write_bytes(b"broken")
    g = ui.make("alice", "bob")
    avatar = lambda name: g.tiles[name].props["child"].children[0].children[0]  # noqa: E731
    assert avatar("alice").image == ("texture", str(icons / "alice"))
    # A picture GDK cannot load: the initials stay
    assert avatar("bob").image is None
    assert avatar("bob").props["text"] == "bob" and avatar("bob").props["show_initials"]


# AccountsService

class AsUser:
    def __init__(self, name, loaded=True, locked=False, icon=None):
        self.name, self.loaded, self.locked, self.icon = name, loaded, locked, icon

    def get_user_name(self):
        return self.name

    def is_loaded(self):
        return self.loaded

    def get_locked(self):
        return self.locked

    def get_icon_file(self):
        return self.icon


class Manager:
    def __init__(self, users, loaded=True, service=True):
        self.users = users
        self.props = types.SimpleNamespace(is_loaded=loaded)
        self.service = service
        self.handlers = []

    def no_service(self):
        return not self.service

    def list_users(self):
        return self.users

    def connect(self, signal, handler):
        self.handlers.append((signal, handler))


def with_accounts_service(mod, monkeypatch, manager):
    ns = types.SimpleNamespace(UserManager=types.SimpleNamespace(get_default=lambda: manager))
    monkeypatch.setattr(mod, "AccountsService", ns)


def test_accounts_service_hides_locked_accounts_and_brings_pictures(mod, ui, monkeypatch):
    pic = ui.tmp / "alice.png"
    pic.write_bytes(b"png")
    manager = Manager([AsUser("alice", icon=str(pic)), AsUser("bob", locked=True),
                       AsUser("carol", icon=str(ui.tmp / "missing.png")),
                       AsUser("dave", loaded=False, locked=True)])
    with_accounts_service(mod, monkeypatch, manager)
    g = ui.make("alice", "bob", "carol", "dave", "erin")
    # erin is unknown to AccountsService and dave not loaded yet: both stay
    assert [a.name for a in g.accounts] == ["alice", "carol", "dave", "erin"]
    assert rows(g) == [["alice", "carol"], ["dave", "erin"]]
    icons = {a.name: a.icon for a in g.accounts}
    assert icons == {"alice": str(pic), "carol": None, "dave": None, "erin": None}


def test_accounts_service_late_leaves_a_single_account(mod, ui, monkeypatch):
    manager = Manager([AsUser("bob", locked=True)], loaded=False)
    with_accounts_service(mod, monkeypatch, manager)
    g = ui.make("alice", "bob")
    assert rows(g) == [["alice", "bob"]]
    ((signal, handler),) = manager.handlers
    assert signal == "notify::is-loaded"
    manager.props.is_loaded = True
    handler(manager, None)
    # Only alice is left to offer: she is logged in without a choice
    assert rows(g) == [] and not g.title.visible
    ui.glib.fire(sources(ui.glib, "idle")[0])
    assert ui.request.read_text() == "alice\n"


@pytest.mark.parametrize("loaded, service", [(False, True), (True, False)])
def test_accounts_service_not_usable(mod, ui, monkeypatch, loaded, service):
    # Not loaded (any more) or no service: what it says is not used
    manager = Manager([AsUser("alice", locked=True), AsUser("bob", locked=True)],
                      loaded=loaded, service=service)
    with_accounts_service(mod, monkeypatch, manager)
    g = ui.make("alice", "bob", "carol")
    g.from_accounts_service(manager)
    assert [a.name for a in g.accounts] == ["alice", "bob", "carol"]
    assert rows(g) == [["alice", "bob"], ["carol"]]


def test_accounts_service_not_asked_in_test_mode(mod, ui, monkeypatch):
    monkeypatch.setattr(mod, "TEST", True)
    monkeypatch.setenv("PLINIUS_GREETER_USERS", "x,y")
    with_accounts_service(mod, monkeypatch, None)  # get_default() would give None
    g = ui.make()
    assert [a.name for a in g.accounts] == ["x", "y"]


# Power

def open_power_menu(mod, ui, g):
    g.on_key_pressed(None, POWER_KEY, 0, 0)
    ui.glib.fire(sources(ui.glib, "timeout", mod.POWER_KEY_LONG_MS)[0])
    g.on_key_released(None, POWER_KEY, 0, 0)
    assert g.power_menu.visible


@pytest.mark.parametrize("first", [None, "alice"])
def test_power_menu_instead_of_a_button(mod, ui, first):
    # No power button on the screen: the power key held down opens Phosh's menu, Power Off and
    # Restart one below the other, and only those
    g = ui.make("alice", "bob", first=first)
    assert [w for w in Widget.made if "power" in w.css] == []
    assert not g.power_menu.visible
    assert list(g.power_entries) == ["PowerOff", "Reboot"]
    open_power_menu(mod, ui, g)
    g.power_entries["Reboot"].emit("clicked")
    assert not g.power_menu.visible
    open_power_menu(mod, ui, g)
    g.power_entries["PowerOff"].emit("clicked")
    target = ("org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager")
    assert ui.bus.calls == [target + ("Reboot", (False,)), target + ("PowerOff", (False,))]


def test_power_menu_closes(mod, ui):
    g = ui.make("alice", "bob")
    # The close button
    open_power_menu(mod, ui, g)
    (close,) = [w for w in Widget.made if "power-close" in w.css]
    close.emit("clicked")
    assert not g.power_menu.visible
    # Escape, which passes on with the menu closed
    open_power_menu(mod, ui, g)
    assert g.on_key_pressed(None, ESCAPE_KEY, 0, 0) is True and not g.power_menu.visible
    assert g.on_key_pressed(None, ESCAPE_KEY, 0, 0) is False
    # A tap beside the card, not one on it
    card = Graphene.Rect().init(24, 400, 428, 200)
    g.power_card.compute_bounds = lambda _target: (True, card)
    open_power_menu(mod, ui, g)
    (tap,) = g.power_menu.controllers
    tap.emit("released", 1, 100.0, 500.0)
    assert g.power_menu.visible
    tap.emit("released", 1, 100.0, 200.0)
    assert not g.power_menu.visible
    g.power_card.compute_bounds = lambda _target: (False, None)
    open_power_menu(mod, ui, g)
    tap.emit("released", 1, 100.0, 500.0)
    assert not g.power_menu.visible
    # The screen going dark
    open_power_menu(mod, ui, g)
    g.blank_screen()
    assert not g.power_menu.visible and ui.bus.calls == []


def test_power_in_test_mode_prints(mod, ui, monkeypatch, capsys):
    monkeypatch.setattr(mod, "TEST", True)
    g = ui.make("alice", "bob")
    g.power("PowerOff")
    assert "plinius-greeter: PowerOff" in capsys.readouterr().out
    assert ui.bus.calls == []


# Dark screen

def test_dark_after_a_while_and_back_on_a_tap(mod, ui):
    g = ui.make("alice", "bob")
    sid = idle_timer(mod, ui.glib)
    ui.glib.fire(sid)
    assert g.blanked and g.blank.visible and ui.light == [0] and g.saved_brightness == 120
    assert idle_timer(mod, ui.glib) is None
    # Touches on the dark screen do not count, a second blanking does nothing
    g.on_any_event(None, None)
    g.blank_screen()
    assert idle_timer(mod, ui.glib) is None and ui.light == [0]
    (tap,) = g.blank.controllers
    tap.emit("released", 1, 10.0, 10.0)
    assert not g.blanked and not g.blank.visible
    assert ui.light == [0, 120] and g.saved_brightness is None
    assert idle_timer(mod, ui.glib) is not None
    g.wake()  # not dark: nothing
    assert ui.light == [0, 120]


def test_any_event_restarts_the_timer(mod, ui):
    g = ui.make("alice", "bob")
    first = idle_timer(mod, ui.glib)
    assert g.on_any_event(None, None) is False
    second = idle_timer(mod, ui.glib)
    assert second != first and first in ui.glib.removed


def test_wake_without_saved_brightness(mod, ui, monkeypatch):
    g = ui.make("alice", "bob")
    monkeypatch.setattr(mod, "set_backlight", lambda v: ui.light.append(v))  # logind refused: None
    g.blank_screen()
    g.wake()
    assert ui.light == [0] and not g.blanked


def test_not_dark_while_an_account_starts(mod, ui):
    g = ui.make("alice", "bob")
    g.tiles["alice"].emit("clicked")
    ui.glib.fire(idle_timer(mod, ui.glib))
    assert not g.blanked and ui.light == []


def test_dark_again_after_a_failed_start(mod, ui):
    g = ui.make("alice", "bob")
    g.tiles["alice"].emit("clicked")
    ui.glib.fire(idle_timer(mod, ui.glib))
    ui.glib.fire(sources(ui.glib, "seconds", mod.START_TIMEOUT_SECONDS)[0])
    assert g.failed
    sid = idle_timer(mod, ui.glib)
    assert sid is not None
    ui.glib.fire(sid)
    assert g.blanked


def test_failed_start_with_the_screen_dark_stays_dark(mod, ui):
    # Darkened by the power key while the account started: the failure does not light it up
    # nor arm the timer (wake() does that at the next touch)
    g = ui.make("alice", "bob")
    g.tiles["alice"].emit("clicked")
    ui.glib.fire(idle_timer(mod, ui.glib))
    g.blank_screen()
    ui.glib.fire(sources(ui.glib, "seconds", mod.START_TIMEOUT_SECONDS)[0])
    assert g.failed and g.blanked and g.idle_timeout == 0
    assert idle_timer(mod, ui.glib) is None


def test_power_key_short_press_toggles_the_screen(mod, ui):
    g = ui.make("alice", "bob")
    assert g.on_key_pressed(None, POWER_KEY, 0, 0) is True
    (timer,) = sources(ui.glib, "timeout", mod.POWER_KEY_LONG_MS)
    # Key repeat while held: one timer
    g.on_key_pressed(None, POWER_KEY, 0, 0)
    assert sources(ui.glib, "timeout") == [timer]
    g.on_key_released(None, POWER_KEY, 0, 0)
    assert timer in ui.glib.removed and g.power_key_timeout == 0
    assert g.blanked and ui.light == [0]
    g.on_key_pressed(None, POWER_KEY, 0, 0)
    g.on_key_released(None, POWER_KEY, 0, 0)
    assert not g.blanked and ui.light == [0, 120]
    assert not g.power_menu.visible


def test_power_key_long_press_asks(mod, ui):
    g = ui.make("alice", "bob")
    g.blank_screen()
    g.on_key_pressed(None, POWER_KEY, 0, 0)
    ui.glib.fire(sources(ui.glib, "timeout", mod.POWER_KEY_LONG_MS)[0])
    assert not g.blanked and g.power_menu.visible
    # Letting go after the long press leaves the screen on
    g.on_key_released(None, POWER_KEY, 0, 0)
    assert not g.blanked
    # The next short press is a short one again
    g.on_key_pressed(None, POWER_KEY, 0, 0)
    g.on_key_released(None, POWER_KEY, 0, 0)
    assert g.blanked


def test_other_keys_pass(mod, ui):
    g = ui.make("alice", "bob")
    assert g.on_key_pressed(None, 0x61, 0, 0) is False
    assert g.on_key_released(None, 0x61, 0, 0) is None
    assert sources(ui.glib, "timeout") == [] and not g.blanked


def test_clock(mod, ui, monkeypatch):
    monkeypatch.setattr(mod, "time_format", lambda: "%H:%M")
    g = ui.make("alice", "bob")
    assert sources(ui.glib, "seconds", 1)
    now = GLib.DateTime.new_now_local()
    assert g.update_clock() == GLib.SOURCE_CONTINUE
    assert g.clock.label in (now.format("%H:%M"), now.add_minutes(1).format("%H:%M"))
    assert g.date.label  # "%A, %B %-d"
    assert "%" not in g.date.label


# ----- the installation's look -----

BACKGROUND_SCHEMA = """<schemalist>
  <schema id="org.gnome.desktop.background" path="/org/gnome/desktop/background/">
    <key name="picture-uri" type="s"><default>'file:///usr/share/backgrounds/gnome/blobs-l.svg'</default></key>
    <key name="picture-uri-dark" type="s"><default>'file:///usr/share/backgrounds/gnome/blobs-d.svg'</default></key>
    <key name="picture-options" type="s"><default>'zoom'</default></key>
    <key name="primary-color" type="s"><default>'#023c88'</default></key>
  </schema>
</schemalist>
"""


@pytest.fixture
def schemas(tmp_path, monkeypatch):
    """The schemas the greeter reads with the installation's override (device-volla-ansuz), compiled
    in tmp_path; schemas(extra) adds an override of its own after it."""
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "Phosh:GNOME")
    d = tmp_path / "schemas"
    d.mkdir()
    shutil.copy(PKG / "plinius-ambient/org.plinius.ambient.gschema.xml", d)
    shutil.copy(PKG / "phosh-plinius/org.plinius.appearance.gschema.xml", d)
    shutil.copy(PKG / "device-volla-ansuz/90-plinius.gschema.override", d)
    (d / "org.gnome.desktop.background.gschema.xml").write_text(BACKGROUND_SCHEMA)

    def compile_(extra=None, without=()):
        if extra:
            (d / "95-test.gschema.override").write_text(extra)
        for name in without:
            (d / name).unlink()
        subprocess.run(["glib-compile-schemas", str(d)], check=True, capture_output=True)
        return Gio.SettingsSchemaSource.new_from_directory(str(d), None, False)
    return compile_


def test_installation_look_of_the_phone(mod, schemas):
    # What device-volla-ansuz sets: black, no picture, Arch blue, dust
    assert mod.installation_look(schemas()) == mod.Look("#000000", None, "#1793d1", "dust", "medium")


def test_installation_look_with_a_picture(mod, schemas, tmp_path):
    light, dark = tmp_path / "light.png", tmp_path / "dark.png"
    light.write_bytes(b"png")
    extra = (f"[org.gnome.desktop.background:Phosh]\npicture-options='zoom'\npicture-uri='{light.as_uri()}'\n"
             f"picture-uri-dark='{dark.as_uri()}'\nprimary-color='#123'\n"
             "[org.plinius.ambient]\neffect='none'\ndensity='high'\n")
    look = mod.installation_look(schemas(extra))
    # The dark picture is missing: the light one
    assert look == mod.Look("#123", str(light), "#1793d1", "none", "high")
    dark.write_bytes(b"png")
    assert mod.installation_look(schemas(extra)).picture == str(dark)


def test_installation_look_keeps_what_cannot_be_used(mod, schemas):
    extra = ("[org.gnome.desktop.background:Phosh]\npicture-options='zoom'\npicture-uri=''\n"
             "picture-uri-dark='file:///nonexistent/picture.png'\nprimary-color='red; } * { color: red'\n"
             "[org.plinius.appearance]\naccent-color='#12345'\n")
    assert mod.installation_look(schemas(extra)) == mod.Look("#000000", None, "#1793d1", "dust", "medium")


def test_installation_look_without_the_schemas(mod, schemas):
    # Only Phosh's schema of the accent is there (no plinius-ambient, no GNOME background)
    source = schemas(without=["org.plinius.ambient.gschema.xml", "org.gnome.desktop.background.gschema.xml"])
    assert mod.installation_look(source) == mod.Look(effect="none")


def test_installation_look_from_the_systems_schemas(mod, monkeypatch, schemas):
    source = schemas()
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(Gio, SettingsSchemaSource=types.SimpleNamespace(
        get_default=lambda: source)))
    assert mod.installation_look().effect == "dust"
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(Gio, SettingsSchemaSource=types.SimpleNamespace(
        get_default=lambda: None)))
    assert mod.installation_look() == mod.Look(effect="none")


def test_css_takes_the_look(mod):
    css = mod.make_css(mod.Look(background="#101010", accent="#abcdef"))
    assert "--accent-bg-color: #abcdef;" in css and "mix(#abcdef, white, 0.35)" in css
    assert ".installation-bg {\n  background-color: #101010;" in css
    # The account picker: plain black, no gradient
    assert "gradient" not in css
    # Every screen has its styles: the dark screen, the unlock page, its keypad, the power menu
    for selector in (".blank {", ".unlock-status {", ".pin-dots {", "button.key {", "button.pin-submit {",
                     "button.pin-mode {", ".power-menu {", ".power-card {", ".power-card button.entry {",
                     "button.power-close {", "button.user-tile {", ".clock {"):
        assert "\n" + selector in css, selector
    assert "{accent}" not in css and "{{" not in css


# ----- the dust -----

def test_dust_field_as_plinius_ambient(mod):
    # As many motes as plinius-ambient for the area (140 on 360x800 at medium), in the screen
    field = mod.DustField(360, 800, "medium", random.Random(1))
    assert len(field.motes) == 140
    assert len(mod.DustField(476, 1058, "high", random.Random(1)).motes) == int(240 * 476 * 1058 / (360 * 800))
    assert len(mod.DustField(10, 10, "low", random.Random(1)).motes) == 17  # a quarter at least
    fronts = [m for m in field.motes if m["z"] > 1]
    assert 0 < len(fronts) < 30 and all(m["z"] <= field.FOREGROUND_MAX for m in field.motes)
    for _ in range(200):
        field.step(1 / 30)
    assert field.time == pytest.approx(200 / 30)
    assert all(0 <= m["x"] <= 360 and 0 <= m["y"] <= 800 for m in field.motes)
    kinds = {k for k, *_ in field.sprites()}
    assert "bokeh" in kinds and kinds - {"bokeh"} <= set(range(field.SHARPNESS_LEVELS))
    for kind, x, y, half, opacity in field.sprites():
        assert 0 <= opacity <= 1 and half > 0
    # Turning the screen keeps the motes where they were, relative to it
    x, y = field.motes[0]["x"], field.motes[0]["y"]
    field.resize(720, 400)
    assert (field.motes[0]["x"], field.motes[0]["y"]) == pytest.approx((2 * x, y / 2))


def test_dust_beam_comes_from_the_upper_left(mod):
    field = mod.DustField(360, 800, "low", random.Random(2))
    cx, cy, half_w, half_l, angle, opacity = field.beam()
    # The texture's top (its source) turned by angle points up and to the left
    a = math.radians(angle)
    top = (cx + half_l * math.sin(a), cy - half_l * math.cos(a))
    assert top[0] < cx and top[1] < 0 and half_w == pytest.approx(0.32 * 360)
    assert 0.17 * 0.85 <= opacity <= 0.17
    # Motes in the beam are lit, those far from it barely
    assert field.beam_distance(72, 0) == 0 and field.beam_distance(360, 0) > half_w
    assert mod.DustField(0, 0, "low", random.Random(3)).unit() == 1.0


def test_dust_field_resize_from_nothing(mod):
    field = mod.DustField(0, 0, "low", random.Random(4))
    field.resize(100, 200)
    assert (field.width, field.height) == (100, 200)


@pytest.mark.parametrize("kind", ["beam", "bokeh", 0, 3])
def test_glow_pixels(mod, kind):
    size = 16
    data = mod.glow_pixels(kind, size)
    height = size * 4 if kind == "beam" else size
    assert len(data) == size * height * 4

    def pixel(col, row):
        return data[(row * size + col) * 4:(row * size + col) * 4 + 4]
    # Premultiplied: colour never above alpha
    assert all(data[i + c] <= data[i + 3] for i in range(0, len(data), 4) for c in range(3))
    if kind == "beam":
        # Bright at its source (top), dark at the far end, strongest in the middle across
        assert pixel(8, 4)[3] > 200 and pixel(8, height - 1)[3] == 0 and pixel(0, 4)[3] < pixel(8, 4)[3]
    elif kind == "bokeh":
        # A disc with a brighter rim, nothing outside of it
        assert pixel(0, 0)[3] == 0 and pixel(8, 8)[3] < pixel(8, 1)[3]
    else:
        assert pixel(0, 0)[3] == 0 and pixel(8, 8)[3] > 200 and pixel(8, 8)[3] > pixel(8, 3)[3]


class Clock:
    def __init__(self):
        self.us = 0

    def get_frame_time(self):
        return self.us


class Snapshot:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, a))


def test_dust_area_ticks_and_draws(mod, monkeypatch):
    area = mod.DustArea("low")
    ticks = []
    removed = []
    area.add_tick_callback = lambda cb: ticks.append(cb) or 5
    area.remove_tick_callback = removed.append
    area.queue_draw = lambda: ticks.append("draw")
    size = [0, 0]
    area.get_width = lambda: size[0]
    area.get_height = lambda: size[1]
    made = []
    monkeypatch.setattr(mod.DustArea, "texture", staticmethod(lambda kind, px: made.append((kind, px)) or kind))
    area.set_running(True)
    area.set_running(True)  # once
    assert ticks == [area.on_tick] and area.tick == 5
    clock = Clock()
    snap = Snapshot()
    area.do_snapshot(snap)  # not laid out yet: nothing
    assert snap.calls == [] and area.field is None
    assert area.on_tick(area, clock) == GLib.SOURCE_CONTINUE  # the first frame only notes the time
    size[:] = [360, 800]
    area.do_snapshot(snap)
    assert {k for k, _ in made} == {"beam", "bokeh", 0, 1, 2, 3}
    names = [c[0] for c in snap.calls]
    assert names[:6] == ["save", "translate", "rotate", "push_opacity", "append_texture", "pop"]
    assert names.count("append_texture") == names.count("push_opacity") >= 2
    # 30 frames a second: a frame of 120 Hz is too soon, the next 30th of a second steps
    clock.us = 8_000
    area.on_tick(area, clock)
    assert area.field.time == 0 and "draw" not in ticks
    clock.us = 34_000
    area.on_tick(area, clock)
    assert area.field.time == pytest.approx(0.034) and "draw" in ticks
    # A long pause (screen dark) steps at most 0.1 s
    clock.us = 5_000_000
    area.on_tick(area, clock)
    assert area.field.time == pytest.approx(0.134)
    # Turned: the same field, resized; the textures stay
    size[:] = [800, 360]
    field = area.field
    area.do_snapshot(Snapshot())
    assert area.field is field and (field.width, field.height) == (800, 360) and len(made) == 6
    area.set_running(False)
    area.set_running(False)
    assert removed == [5] and area.tick == 0
    # Unchanged size: the same field; a mote too faint to see is left out
    field.sprites = lambda: [(0, 1.0, 1.0, 2.0, 0.0), (0, 1.0, 1.0, 2.0, 0.5)]
    snap = Snapshot()
    area.do_snapshot(snap)
    assert area.field is field and [c[0] for c in snap.calls].count("append_texture") == 2  # the beam + one
    area.field = None
    ticks.clear()
    clock.us = 6_000_000
    area.on_tick(area, clock)  # no field yet: draws only
    assert ticks == ["draw"]


def test_dust_area_texture(mod, monkeypatch):
    made = []
    monkeypatch.setattr(mod.Gdk, "MemoryTexture", types.SimpleNamespace(
        new=lambda w, h, fmt, data, stride: made.append((w, h, data.get_size(), stride))))
    mod.DustArea.texture("beam", 8)
    mod.DustArea.texture(1, 8)
    assert made == [(8, 32, 8 * 32 * 4, 32), (8, 8, 8 * 8 * 4, 32)]


# ----- backlight -----

@pytest.fixture
def sysfs(mod, tmp_path, monkeypatch):
    root = tmp_path / "sys"

    def mapped(path):
        path = str(path)
        assert path.startswith("/sys/class"), path
        return str(root) + path[len("/sys"):]
    real_open = open
    monkeypatch.setattr(mod, "os", fakes.Proxy(
        os, listdir=lambda p: os.listdir(mapped(p)),
        path=fakes.Proxy(os.path, exists=lambda p: os.path.exists(mapped(p)))))
    monkeypatch.setattr(mod, "open", lambda p, *a, **k: real_open(mapped(p), *a, **k),
                        raising=False)
    bus = Bus()
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(Gio, bus_get_sync=lambda _t: bus))
    monkeypatch.setattr(mod, "TEST", False)

    def device(subsystem, name, brightness="77\n"):
        d = root / "class" / subsystem / name
        d.mkdir(parents=True)
        if brightness is not None:
            (d / "brightness").write_text(brightness)
        return d
    return types.SimpleNamespace(device=device, bus=bus)


def test_backlight_device_prefers_backlight_class(mod, sysfs):
    assert mod.backlight_device() is None
    sysfs.device("leds", "lcd-backlight")
    sysfs.device("leds", "red")
    assert mod.backlight_device() == ("leds", "lcd-backlight")
    sysfs.device("backlight", "panel1", brightness=None)  # no brightness file
    sysfs.device("backlight", "panel0")
    assert mod.backlight_device() == ("backlight", "panel0")


def test_backlight_device_only_the_lcd_led(mod, sysfs):
    sysfs.device("leds", "flashlight")
    assert mod.backlight_device() is None


def test_set_backlight_through_logind(mod, sysfs):
    sysfs.device("leds", "lcd-backlight", "812\n")
    assert mod.set_backlight(0) == 812
    assert sysfs.bus.calls == [("org.freedesktop.login1", "/org/freedesktop/login1/session/auto",
                                "org.freedesktop.login1.Session", "SetBrightness",
                                ("leds", "lcd-backlight", 0))]


def test_set_backlight_errors(mod, sysfs, capsys, monkeypatch):
    assert mod.set_backlight(0) is None  # no device
    d = sysfs.device("leds", "lcd-backlight", "n/a\n")
    assert mod.set_backlight(0) is None
    assert "brightness" in capsys.readouterr().err
    (d / "brightness").write_text("10\n")
    sysfs.bus.fail = True
    assert mod.set_backlight(0) is None
    assert sysfs.bus.calls == []
    monkeypatch.setattr(mod, "TEST", True)
    sysfs.bus.fail = False
    assert mod.set_backlight(0) is None and sysfs.bus.calls == []


# ----- main -----

class App:
    def __init__(self, **kw):
        self.kw = kw
        self.handlers = {}
        self.argv = None

    def connect(self, signal, handler):
        self.handlers[signal] = handler

    def run(self, argv):
        self.argv = argv
        return 7


@pytest.mark.parametrize("windowed", [False, True])
def test_main_activates_a_fullscreen_window(mod, monkeypatch, windowed):
    made = []
    monkeypatch.setattr(mod.Adw, "Application", lambda **kw: made.append(App(**kw)) or made[-1])
    providers = []

    class Provider:
        def load_from_string(self, css):
            providers.append(css)
    monkeypatch.setattr(mod.Gtk, "CssProvider", Provider)
    windows = []

    class Window:
        def __init__(self, app, look):
            self.app, self.look, self.calls = app, look, []
            windows.append(self)

        def fullscreen(self):
            self.calls.append("fullscreen")

        def present(self):
            self.calls.append("present")
    monkeypatch.setattr(mod, "Greeter", Window)
    look = mod.Look(accent="#ff0000")
    monkeypatch.setattr(mod, "installation_look", lambda: look)
    setup = fakes.recorder()
    monkeypatch.setattr(mod, "setup_locale", setup)
    feedback_setup = fakes.recorder()
    monkeypatch.setattr(mod, "setup_feedback", feedback_setup)
    if windowed:
        monkeypatch.setenv("PLINIUS_GREETER_WINDOWED", "1")
    else:
        monkeypatch.delenv("PLINIUS_GREETER_WINDOWED", raising=False)
    monkeypatch.setattr(sys, "argv", ["plinius-greeter", "--ignored"])
    assert mod.main() == 7
    (app,) = made
    assert setup.calls == [()] and feedback_setup.calls == [()]
    assert app.kw["application_id"] == "org.plinius.Greeter"
    assert app.argv == ["plinius-greeter"]
    app.handlers["activate"](app)
    assert providers == [mod.make_css(look)] and "--accent-bg-color: #ff0000;" in providers[0]
    assert windows[0].app is app and windows[0].look is look
    assert windows[0].calls == (["present"] if windowed else ["fullscreen", "present"])


def test_removed_password_is_no_first_start(config_mod, root):
    # "Set at next login" in Settings empties the field (passwd -d): the greeter must not offer
    # the account to whoever holds the phone
    root.users(entry("alice", 1000))
    without_password(root, field="")
    root.run_dir.mkdir()
    config = config_mod.make_config()
    assert config["initial_session"]["user"] == "alice"
    assert not root.first.exists()
