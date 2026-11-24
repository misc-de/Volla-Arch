# User rename (plinius-rename-user via pkexec, rename-user-boot at boot as root, dconf-rename in
# the session) and moving /home (plinius-home-move, plinius-home-init). The account tools
# (id, getent, usermod, groupmod, setpriv ...) are stubs; homes and system files are in tmp_path.

import os

import pytest

from conftest import PKG
from system_helpers import rewritten_copy, run

ACCOUNT = PKG / "plinius-account"
HOME = PKG / "plinius-home"

# Name -> (uid, primary group). The id and getent stubs answer from this table.
USERS = "root:0:root daemon:2:daemon anna:1000:anna carl:1001:users"


def account_stubs(fake_bin, tmp_path, homes):
    """id/getent for the users in USERS (plus renamed ones in RENAMED, as "old=new")."""
    table = tmp_path / "users"
    table.write_text("".join(f"{u}\n" for u in USERS.split()))
    fake_bin.stub("id", f'''
lookup() {{ grep -m1 "^$1:" "{table}"; }}
case $1 in
-u) [ $# -eq 1 ] && {{ echo "${{FAKE_UID:-0}}"; exit; }}
    l=$(lookup "$2") || exit 1; echo "$l" | cut -d: -f2 ;;
-un) l=$(grep -m1 "^[^:]*:$2:" "{table}") || exit 1; echo "${{l%%:*}}" ;;
-g) l=$(lookup "$2") || exit 1; echo "$l" | cut -d: -f2 ;;
-gn) l=$(lookup "$2") || exit 1; echo "$l" | cut -d: -f3 ;;
*) exit 1 ;;
esac''')
    fake_bin.stub("getent", f'''
case $1 in
passwd) l=$(grep "^$2:" "{table}") || exit 2
    h=$(grep "^$2=" "{homes}" | cut -d= -f2-); echo "$2:x:$(echo "$l" | cut -d: -f2):1000::$h:/bin/bash" ;;
group) grep -q "^$2:" "{tmp_path}/groups" 2>/dev/null || exit 2; echo "$2:x:1000:" ;;
esac''')
    return table


# --- plinius-rename-user (pkexec, the caller's own account only) ---

class RenameRequest:
    def __init__(self, tmp_path, fake_bin):
        self.tmp = tmp_path
        self.bin = fake_bin
        self.request = tmp_path / "var" / "plinius-account" / "rename"
        self.home = tmp_path / "home"
        self.home.mkdir()
        homes = tmp_path / "homes"
        homes.write_text(f"anna={self.home}/anna\n")
        account_stubs(fake_bin, tmp_path, homes)
        fake_bin.stub("gettext", 'printf "%s" "$1"')
        self.script = rewritten_copy(ACCOUNT / "plinius-rename-user", tmp_path / "rename-user", {
            "/var/lib/plinius-account/rename": self.request,
            "/etc/locale.conf": tmp_path / "locale.conf",
            "/home/$new": f"{self.home}/$new",
        }, check_left=("/etc/", "/var/", "/home/"))

    def run(self, *args, **env):
        env.setdefault("PKEXEC_UID", "1000")
        return run([self.script, *args], self.bin.env(isolated=True, **env))


@pytest.fixture
def request_tool(tmp_path, fake_bin):
    return RenameRequest(tmp_path, fake_bin)


def test_rename_request_written(request_tool):
    r = request_tool.run("anna", "bob")
    assert r.returncode == 0, r.stderr
    assert request_tool.request.read_text() == "anna bob\n"
    assert oct(request_tool.request.parent.stat().st_mode & 0o777) == "0o755"


@pytest.mark.parametrize("new", ["", "Bob", "1bob", "-bob", "bo b", "bob/../root", "../bob",
                                 "bob:0", "bob$", "bob\n", "b" * 33, "bob;reboot", "böb", ".bob"])
def test_rename_rejects_bad_new_names(request_tool, new):
    r = request_tool.run("anna", new)
    assert r.returncode == 1
    assert not request_tool.request.exists()


@pytest.mark.parametrize("new", ["_svc", "b" * 32, "bob-2", "b_1"])
def test_rename_accepts_valid_names(request_tool, new):
    assert request_tool.run("anna", new).returncode == 0
    assert request_tool.request.read_text() == f"anna {new}\n"


def test_rename_only_own_account_under_pkexec(request_tool):
    r = request_tool.run("carl", "bob")  # caller is uid 1000 = anna
    assert r.returncode == 1 and "only your own account" in r.stderr
    assert not request_tool.request.exists()


def test_rename_refuses_system_user_as_root(request_tool):
    r = request_tool.run("daemon", "bob", PKEXEC_UID="")
    assert r.returncode == 1 and "not a regular user" in r.stderr
    assert not request_tool.request.exists()


def test_rename_refuses_unknown_user(request_tool):
    r = request_tool.run("nobody2", "bob", PKEXEC_UID="")
    assert r.returncode == 1 and "does not exist" in r.stderr


def test_rename_refuses_taken_names(request_tool, tmp_path):
    assert request_tool.run("anna", "carl").returncode == 1
    (tmp_path / "groups").write_text("bob:\n")
    assert request_tool.run("anna", "bob").returncode == 1
    (tmp_path / "groups").write_text("")
    (request_tool.home / "dora").mkdir()
    assert request_tool.run("anna", "dora").returncode == 1
    assert not request_tool.request.exists()


def test_rename_needs_root(request_tool):
    r = request_tool.run("anna", "bob", FAKE_UID="1000")
    assert r.returncode == 1 and not request_tool.request.exists()


@pytest.mark.parametrize("args", [["anna"], ["anna", "bob", "extra"], []])
def test_rename_usage(request_tool, args):
    r = request_tool.run(*args)
    assert r.returncode == 1 and "usage" in r.stderr


def test_rename_status_and_cancel(request_tool):
    assert request_tool.run("--status").stdout == ""
    assert request_tool.run("anna", "bob").returncode == 0
    assert request_tool.run("--status").stdout == "anna bob\n"
    # Somebody else cannot withdraw it
    r = request_tool.run("--cancel", PKEXEC_UID="1001")
    assert r.returncode == 1 and request_tool.request.exists()
    assert request_tool.run("--cancel").returncode == 0
    assert not request_tool.request.exists()


# --- rename-user-boot (root, before any login) ---

class BootRename:
    def __init__(self, tmp_path, fake_bin):
        self.tmp = tmp_path
        self.bin = fake_bin
        self.request = tmp_path / "var" / "lib" / "plinius-account" / "rename"
        self.request.parent.mkdir(parents=True)
        self.home = tmp_path / "home"
        self.old_home = self.home / "anna"
        self.old_home.mkdir(parents=True)
        self.etc = tmp_path / "etc"
        self.etc.mkdir()
        self.var = tmp_path / "var"
        for d in ("spool/mail", "spool/cron", "lib/systemd/linger", "lib/AccountsService/users",
                  "lib/AccountsService/icons"):
            (self.var / d).mkdir(parents=True)
        self.homes = tmp_path / "homes"
        self.homes.write_text(f"anna={self.old_home}\n")
        account_stubs(fake_bin, tmp_path, self.homes)
        fake_bin.stub("pgrep", "exit 1")
        fake_bin.stub("groupmod")
        # usermod -d NEW -m USER moves the home, as the real one does
        fake_bin.stub("usermod", f'[ "$1" = -d ] && {{ mv "{self.old_home}" "$2" || exit 1; }}\n'
                      f'[ "$1" = -l ] && sed -i "s/^$3:/$2:/" "{tmp_path}/users"; exit 0')
        # setpriv: drop its options, run the command (as the test user, not as root)
        fake_bin.stub("setpriv", 'while [ "$1" != -- ]; do shift; done; shift; exec "$@"')
        self.script = rewritten_copy(ACCOUNT / "rename-user-boot", tmp_path / "rename-user-boot", {
            "/var/lib/plinius-account/rename": self.request,
            "home_new=/home/$new": f"home_new={self.home}/$new",
            "/etc/subuid /etc/subgid": f"{self.etc}/subuid {self.etc}/subgid",
            "/var/spool/mail /var/spool/cron /var/lib/systemd/linger":
                f"{self.var}/spool/mail {self.var}/spool/cron {self.var}/lib/systemd/linger",
            "as=/var/lib/AccountsService": f"as={self.var}/lib/AccountsService",
            "/etc/greetd/config.toml": self.etc / "greetd.toml",
            "/var/lib/plinius-greeter/last-user": self.var / "lib/plinius-greeter/last-user",
        }, check_left=("/etc/", "/var/"))

    def run(self, request="anna bob\n"):
        self.request.write_text(request)
        return run([self.script], self.bin.env(isolated=True))


@pytest.fixture
def boot(tmp_path, fake_bin):
    return BootRename(tmp_path, fake_bin)


@pytest.mark.parametrize("request_line", ["anna ../root\n", "anna bob extra\n", "../x bob\n",
                                          "anna\n", "anna Bob\n", "anna b$(reboot)\n",
                                          "anna bob/x\n", "anna 'b'\n", "\n"])
def test_boot_rename_rejects_bad_requests(boot, request_line):
    r = boot.run(request_line)
    assert r.returncode == 1 and "invalid request" in r.stderr
    assert boot.bin.calls("usermod") == []
    # Removed in any case, so a bad request cannot block every boot
    assert not boot.request.exists()


def test_boot_rename_refuses_system_user(boot):
    r = boot.run("daemon bob\n")
    assert r.returncode == 1 and "not a regular user" in r.stderr
    assert boot.bin.calls("usermod") == []


def test_boot_rename_refuses_when_user_runs_or_name_taken(boot, fake_bin):
    fake_bin.stub("pgrep", "exit 0")
    assert boot.run().returncode == 1
    fake_bin.stub("pgrep", "exit 1")
    assert boot.run("anna carl\n").returncode == 1
    (boot.home / "bob").mkdir()
    assert boot.run().returncode == 1
    assert fake_bin.calls("usermod") == []


def test_boot_rename_rolls_back_failed_group_rename(boot, fake_bin):
    fake_bin.stub("groupmod", "exit 1")
    r = boot.run()
    assert r.returncode == 1 and "group anna could not be renamed" in r.stderr
    assert fake_bin.calls("usermod") == [["-l", "bob", "anna"], ["-l", "anna", "bob"]]
    assert boot.old_home.is_dir()


def test_boot_rename_rolls_back_failed_home_move(boot, fake_bin):
    fake_bin.stub("usermod", '[ "$1" = -d ] && exit 1; exit 0')
    r = boot.run()
    assert r.returncode == 1 and "could not be moved" in r.stderr
    assert fake_bin.calls("groupmod") == [["-n", "bob", "anna"], ["-n", "anna", "bob"]]
    assert fake_bin.calls("usermod")[-1] == ["-l", "anna", "bob"]


def test_boot_rename_system_files(boot):
    (boot.etc / "subuid").write_text("anna:100000:65536\nannabel:165536:65536\n")
    (boot.etc / "subgid").write_text("anna:100000:65536\n")
    (boot.var / "spool/mail/anna").write_text("mail")
    (boot.var / "lib/systemd/linger/anna").touch()
    users = boot.var / "lib/AccountsService/users"
    icons = boot.var / "lib/AccountsService/icons"
    (icons / "anna").write_bytes(b"png")
    (users / "anna").write_text(f"[User]\nIcon={icons}/anna\nLanguage=de\n")
    (boot.etc / "greetd.toml").write_text('[initial_session]\nuser = "anna"\n'
                                          '[default_session]\nuser = "annabel"\n')
    r = boot.run()
    assert r.returncode == 0, r.stderr
    assert (boot.etc / "subuid").read_text() == "bob:100000:65536\nannabel:165536:65536\n"
    assert (boot.etc / "subgid").read_text() == "bob:100000:65536\n"
    assert (boot.var / "spool/mail/bob").read_text() == "mail"
    assert (boot.var / "lib/systemd/linger/bob").exists()
    assert (users / "bob").read_text() == f"[User]\nIcon={icons}/bob\nLanguage=de\n"
    assert (icons / "bob").read_bytes() == b"png"
    assert (boot.etc / "greetd.toml").read_text() == ('[initial_session]\nuser = "bob"\n'
                                                      '[default_session]\nuser = "annabel"\n')
    assert not boot.request.exists()


@pytest.mark.parametrize("last, after", [("anna\n", "bob\n"), ("annabel\n", "annabel\n"),
                                         ("anna2\n", "anna2\n"), (None, None)])
def test_boot_rename_greeter_last_user(boot, last, after):
    # The greeter offers the renamed account first only if it was the last one; another
    # account (also one whose name starts the same) stays, a missing file is not created
    f = boot.var / "lib/plinius-greeter/last-user"
    f.parent.mkdir(parents=True)
    if last is not None:
        f.write_text(last)
    r = boot.run()
    assert r.returncode == 0, r.stderr
    assert (f.read_text() if f.exists() else None) == after


def test_boot_rename_settings_and_symlinks_in_the_home(boot):
    old, new = boot.old_home, boot.home / "bob"
    (old / ".config/app").mkdir(parents=True)
    (old / ".config/app/app.conf").write_text(
        f"music={old}/Music\nhome={old}\nother={boot.home}/annabel/x\n")
    (old / ".config/dconf").mkdir()
    (old / ".config/dconf/user").write_bytes(b"\0gvdb")
    (old / ".config/repo/.git").mkdir(parents=True)
    (old / ".config/repo/.git/config").write_text(f"path={old}/x\n")
    (old / ".cache/tracker3").mkdir(parents=True)
    (old / ".cache/list").write_text(f"{old}/x\n")
    (old / "Documents").mkdir()
    (old / "Documents/notes.txt").write_text(f"{old}/x\n")
    (old / "Documents/music").symlink_to(old / "Music")
    (old / "Documents/home").symlink_to(old)
    (old / "Documents/other").symlink_to(boot.home / "annabel/x")
    (old / "Documents/relative").symlink_to("../Music")
    r = boot.run()
    assert r.returncode == 0, r.stderr
    assert not old.exists()
    assert (new / ".config/app/app.conf").read_text() == (
        f"music={new}/Music\nhome={new}\nother={boot.home}/annabel/x\n")
    # Git working copies, the cache and documents are not settings: unchanged
    assert (new / ".config/repo/.git/config").read_text() == f"path={old}/x\n"
    assert (new / ".cache/list").read_text() == f"{old}/x\n"
    assert (new / "Documents/notes.txt").read_text() == f"{old}/x\n"
    assert os.readlink(new / "Documents/music") == f"{new}/Music"
    assert os.readlink(new / "Documents/home") == str(new)
    assert os.readlink(new / "Documents/other") == f"{boot.home}/annabel/x"
    assert os.readlink(new / "Documents/relative") == "../Music"
    assert (new / ".config/plinius-account/dconf-rename").read_text() == f"{old}\n"
    assert not (new / ".cache/tracker3").exists()


def test_boot_rename_old_home_with_regex_characters(boot):
    # The home comes from passwd and is matched literally (escaped for sed's pattern)
    odd = boot.home / "a.n|a"
    boot.old_home.rename(odd)
    boot.old_home = odd
    boot.homes.write_text(f"anna={odd}\n")
    boot.bin.stub("usermod", f'[ "$1" = -d ] && {{ mv "{odd}" "$2" || exit 1; }}\n'
                  f'[ "$1" = -l ] && sed -i "s/^$3:/$2:/" "{boot.tmp}/users"; exit 0')
    (odd / ".conf").write_text(f"a={odd}/x\nb={boot.home}/axn|a/x\n")
    r = boot.run()
    assert r.returncode == 0, r.stderr
    new = boot.home / "bob"
    assert (new / ".conf").read_text() == f"a={new}/x\nb={boot.home}/axn|a/x\n"


# --- dconf-rename (session, first login after the rename) ---

def dconf_run(tmp_path, fake_bin, old, dump):
    home = tmp_path / "home" / "bob"
    marker = home / ".config/plinius-account/dconf-rename"
    marker.parent.mkdir(parents=True)
    marker.write_text(old + "\n")
    (tmp_path / "dump").write_text(dump)
    loaded = tmp_path / "loaded"
    fake_bin.stub("dconf", f'case $1 in dump) cat "{tmp_path}/dump" ;; '
                  f'load) cat > "{loaded}" ;; esac')
    r = run(["sh", ACCOUNT / "dconf-rename"], fake_bin.env(isolated=True, HOME=home))
    return r, home, marker, loaded


def test_dconf_rename_rewrites_only_keys_with_the_old_home(tmp_path, fake_bin):
    old = "/home/anna"
    dump = ("[org/gnome/desktop/background]\npicture-uri='file:///home/anna/Pictures/a.png'\n"
            "color='#000000'\n\n[org/gnome/nautilus]\nlast='/home/anna'\n"
            "other='/home/annabel/x'\n\n[org/locked]\nkey='nothing'\n")
    r, home, marker, loaded = dconf_run(tmp_path, fake_bin, old, dump)
    assert r.returncode == 0, r.stderr
    assert loaded.read_text() == (
        f"[org/gnome/desktop/background]\npicture-uri='file://{home}/Pictures/a.png'\n"
        f"[org/gnome/nautilus]\nlast='{home}'\nother='/home/annabel/x'\n")
    assert not marker.exists()


def test_dconf_rename_nothing_to_change(tmp_path, fake_bin):
    r, _, marker, loaded = dconf_run(tmp_path, fake_bin, "/home/anna", "[a]\nb='c'\n")
    assert r.returncode == 0 and not loaded.exists() and not marker.exists()
    assert fake_bin.calls("dconf") == [["dump", "/"]]


def test_dconf_rename_special_characters_are_literal(tmp_path, fake_bin):
    old = "/home/a.n&a"
    dump = "[x]\nk='/home/a.n&a/f'\nl='/home/axn&a/f'\n"
    r, home, _, loaded = dconf_run(tmp_path, fake_bin, old, dump)
    assert r.returncode == 0, r.stderr
    assert loaded.read_text() == f"[x]\nk='{home}/f'\n"


# --- plinius-home-move (root over SSH; PLINIUS_HOME_ROOT is its own test switch) ---

class HomeMove:
    def __init__(self, tmp_path, fake_bin):
        self.root = tmp_path / "root"
        self.src = self.root / "home" / "plinius"
        self.dst = self.root / "userdata" / "home" / "plinius"
        self.src.mkdir(parents=True)
        (self.root / "userdata").mkdir()
        self.generator = self.root / "etc/systemd/system-generators/systemd-gpt-auto-generator"
        self.bin = fake_bin
        fake_bin.stub("mountpoint", "exit ${NOT_MOUNTED:-0}")
        # Different devices for /home and /userdata unless SAME_DEVICE is set
        fake_bin.stub("stat", 'if [ "$1" = -c ] && [ "$2" = %d ]; then\n'
                      '  case $3 in */userdata) echo 2 ;; *) echo "${SAME_DEVICE:-1}" ;; esac\n'
                      'else exec /usr/bin/stat "$@"; fi')
        fake_bin.stub("du", 'printf "%s\\t%s\\n" "${NEED_KB:-1000}" "$2"')
        fake_bin.stub("df", 'printf "Avail\\n%s\\n" "${FREE_KB:-50000}"')
        fake_bin.stub("rsync", 'case "$*" in *--dry-run*) printf "%s" "${RSYNC_DIFF:-}" ;; '
                      '*) mkdir -p "${@: -1}" ;; esac')
        fake_bin.stub("systemctl")
        fake_bin.stub("fuser", "exit ${BUSY:-1}")
        fake_bin.stub("id", "echo 32011")
        fake_bin.stub("sync")

    def run(self, *args, **env):
        return run(["bash", HOME / "plinius-home-move", *args],
                   self.bin.env(isolated=True, PLINIUS_HOME_ROOT=self.root, **env))


@pytest.fixture
def move(tmp_path, fake_bin):
    return HomeMove(tmp_path, fake_bin)


@pytest.mark.parametrize("args", [[], ["start"], ["--help"]])
def test_home_move_usage(move, args):
    r = move.run(*args)
    assert r.returncode == 1 and "usage" in r.stderr


def test_home_move_copy(move):
    r = move.run("copy")
    assert r.returncode == 0, r.stderr
    assert move.bin.calls("rsync") == [["-aHAX", "--numeric-ids", "--delete", f"{move.src}/",
                                        f"{move.dst}/"]]
    assert move.bin.calls("systemctl") == []


@pytest.mark.parametrize("env,message", [({"NOT_MOUNTED": 1}, "no mount point"),
                                         ({"SAME_DEVICE": 2}, "already on the data partition"),
                                         ({"NEED_KB": 60000}, "only 50000 kB free")])
def test_home_move_copy_refuses(move, env, message):
    r = move.run("copy", **env)
    assert r.returncode == 1 and message in r.stderr
    assert move.bin.calls("rsync") == []


def test_home_move_copy_without_source(move):
    move.src.rmdir()
    r = move.run("copy")
    assert r.returncode == 1 and "does not exist" in r.stderr


def test_home_move_finish_needs_a_copy_first(move):
    r = move.run("finish")
    assert r.returncode == 1 and "copy" in r.stderr
    assert move.bin.calls("systemctl") == []


def test_home_move_finish(move):
    move.dst.mkdir(parents=True)
    r = move.run("finish")
    assert r.returncode == 0, r.stderr
    assert move.bin.calls("systemctl") == [["stop", "greetd"], ["stop", "user@32011.service"],
                                           ["enable", "home.mount"]]
    assert os.readlink(move.generator) == "/dev/null"
    dry = [c for c in move.bin.calls("rsync") if "--dry-run" in c]
    assert dry and "--checksum" in dry[0]


def test_home_move_finish_refuses_while_home_is_busy(move):
    move.dst.mkdir(parents=True)
    r = move.run("finish", BUSY=0)
    assert r.returncode == 1 and "still in use" in r.stderr
    assert ["enable", "home.mount"] not in move.bin.calls("systemctl")
    assert not os.path.lexists(move.generator)


def test_home_move_finish_refuses_an_incomplete_copy(move):
    move.dst.mkdir(parents=True)
    r = move.run("finish", RSYNC_DIFF=">fc.T...... .config/x\n")
    assert r.returncode == 1 and "differs" in r.stderr
    assert ["enable", "home.mount"] not in move.bin.calls("systemctl")
    assert not os.path.lexists(move.generator)


def test_home_move_undo(move):
    move.generator.parent.mkdir(parents=True)
    move.generator.symlink_to("/dev/null")
    r = move.run("undo")
    assert r.returncode == 0, r.stderr
    assert move.bin.calls("systemctl") == [["disable", "home.mount"]]
    assert not os.path.lexists(move.generator)


# --- plinius-home-init (first start: the home from /etc/skel) ---

def home_init(tmp_path, fake_bin):
    userdata = tmp_path / "userdata"
    skel = tmp_path / "skel"
    (skel / ".config").mkdir(parents=True)
    (skel / ".bashrc").write_text("# skel\n")
    script = rewritten_copy(HOME / "plinius-home-init", tmp_path / "init", {
        "/userdata/home": userdata / "home",
        "/etc/skel": skel,
    }, check_left=("/userdata", "/etc/"))
    fake_bin.stub("id", 'case $1 in -u) echo 32011 ;; -g) echo 32011 ;; esac')
    fake_bin.stub("chown")
    fake_bin.stub("sync")
    return script, userdata / "home"


def test_home_init_builds_under_another_name(tmp_path, fake_bin):
    script, home = home_init(tmp_path, fake_bin)
    # A half-made copy of an interrupted start is thrown away, not taken over
    (home / ".plinius.new" / "junk").mkdir(parents=True)
    r = run([script], fake_bin.env(isolated=True))
    assert r.returncode == 0, r.stderr
    user = home / "plinius"
    assert (user / ".bashrc").read_text() == "# skel\n" and (user / ".config").is_dir()
    assert not (user / "junk").exists() and not (home / ".plinius.new").exists()
    assert oct(user.stat().st_mode & 0o777) == "0o700"
    assert fake_bin.calls("chown") == [["-R", "32011:32011", str(home / ".plinius.new")]]
