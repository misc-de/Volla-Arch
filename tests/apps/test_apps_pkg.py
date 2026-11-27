# plinius-pkg (pkgbuilds/plinius-software): the root helper's checks and decisions, tested as
# single functions (its top level needs root and starts a system unit): package name check,
# exit code mapping of failed pacman runs, the --overwrite rule for stray files, the channel
# switch in pacman.conf, the battery rule, the lock and the install order.
#
# Every program is a stub on an isolated PATH; the fixed system paths are replaced by paths
# in tmp_path before the functions are sourced.

import re
import subprocess

import pytest

from conftest import PKG, bash_functions

SCRIPT = PKG / "plinius-software" / "plinius-pkg"


def functions(*names, replace=None):
    code = bash_functions(SCRIPT, "die", *names)
    for old, new in (replace or {}).items():
        assert old in code, f"{old} no longer in plinius-pkg"
        code = code.replace(old, str(new))
    return code


@pytest.fixture
def bash(fake_bin, tmp_path):
    def run(code, *args, **env):
        prelude = (f"set -euo pipefail\nRUN={tmp_path}\nOUT={tmp_path}/pacman.out\n"
                   "SANDBOX=()\nMIN_BATTERY=10\n")
        return subprocess.run(["bash", "-c", prelude + code, "plinius-pkg", *args],
                              env=fake_bin.env(isolated=True, **env), capture_output=True, text=True)
    return run


# --- Package names ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["glibc", "gtk4", "lib32-foo", "xorg-server", "python-gobject",
                                  "c++utilities", "foo@bar", "_x", "0ad", "linux-firmware.x"])
def test_valid_package_names(bash, name):
    assert bash(functions("check_pkgnames") + 'check_pkgnames "$@"', "glibc", name).returncode == 0


@pytest.mark.parametrize("name", ["-Syu", "--overwrite=*", "Glibc", "a b", "", "x\ny", "../etc",
                                  ".hidden", "foo/bar", "foo;rm", "füß", "-"])
def test_invalid_package_names_are_refused(bash, name):
    r = bash(functions("check_pkgnames") + 'check_pkgnames "$@"; echo passed', "glibc", name)
    assert r.returncode == 1 and "passed" not in r.stdout
    assert "invalid package name" in r.stderr


# --- Exit code of a failed pacman run --------------------------------------------------------

@pytest.mark.parametrize("output, code", [
    (":: plinius-freigabe-5-1 and glibc-2.45-1 are in conflict\n", 8),
    (":: glibc-2.45-1 and plinius-freigabe-5-1 are in conflict\n", 8),
    (":: foo-1-1 and bar-2-1 are in conflict\n", 1),
    ("error: failed retrieving file 'x.pkg.tar.zst' from mirror : The requested URL returned error: 404\n", 4),
    ("error: failed to commit transaction (conflicting files)\n", 1),
    ("", 1),
])
def test_classify(bash, tmp_path, output, code):
    (tmp_path / "pacman.out").write_text(output)
    r = bash(functions("held", "classify") + "classify")
    assert r.returncode == code


# --- Stray files in the way of our packages ----------------------------------------------------

PACMAN_FILES = """
case "$1" in
	-Slq) [ "$2" = plinius ] && printf '%s\\n' muga plinius-software; exit 0 ;;
	-Qqo) case "$2" in /usr/owned*) echo other; exit 0 ;; esac; exit 1 ;;
esac
exit 2
"""


@pytest.fixture
def stray(bash, fake_bin, tmp_path):
    fake_bin.stub("pacman", PACMAN_FILES)

    def run(output):
        (tmp_path / "pacman.out").write_text(output)
        return bash(functions("stray_overwrites") + "stray_overwrites")
    return run


def test_stray_files_of_our_packages_get_overwrite_options(stray):
    r = stray("error: failed to commit transaction (conflicting files)\n"
              "muga: /usr/lib/python3.14/site-packages/muga/panorama.py exists in filesystem\n"
              "plinius-software: /usr/share/plinius-software/old file.txt exists in filesystem\n"
              "Errors occurred, no packages were upgraded.\n")
    assert r.returncode == 0
    assert r.stdout.splitlines() == ["--overwrite=usr/lib/python3.14/site-packages/muga/panorama.py",
                                     "--overwrite=usr/share/plinius-software/old file.txt"]


@pytest.mark.parametrize("line", [
    "glibc: /usr/lib/libc.so.6 exists in filesystem",                  # not a package of ours
    "muga: /usr/owned/file exists in filesystem",                      # another package owns it
    "muga: /usr/lib/muga/a.py exists in filesystem (owned by other)",  # pacman says so itself
    "muga: /usr/lib/muga/*.py exists in filesystem",                   # would be a pattern
    "muga: /usr/lib/muga/[a].py exists in filesystem",
    "muga: relative/path exists in filesystem",
])
def test_stray_files_refused_when_anything_else_is_in_the_way(stray, line):
    r = stray(f"muga: /usr/lib/muga/ok.py exists in filesystem\n{line}\n")
    assert r.returncode != 0 and r.stdout == ""


def test_stray_files_none(stray):
    assert stray("error: something else\n").returncode != 0


# --- Channel switch in pacman.conf -----------------------------------------------------------

CONF = """\
[options]
HoldPkg = pacman glibc
[core]
Server = https://mirror.example/core/beta/aarch64
[plinius]
SigLevel = Required DatabaseOptional
Server = https://plinius.example/repo/$arch
#Server = https://old.example/repo/$arch
[extra]
Server = https://mirror.example/$arch/extra
"""


@pytest.fixture
def channel(bash, fake_bin, tmp_path):
    fake_bin.stub("chown")
    conf = tmp_path / "etc" / "pacman.conf"
    conf.parent.mkdir()
    conf.write_text(CONF)
    conf.chmod(0o640)
    code = functions("set_channel", replace={"/etc/.pacman.conf.XXXXXX": tmp_path / "etc" / ".pacman.conf.XXXXXX",
                                             "/etc/pacman.conf": conf})

    def run(want):
        return bash(code + f"set_channel {want}")
    return conf, run


def test_channel_beta_and_back(channel):
    conf, run = channel
    r = run("beta")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "Server = https://plinius.example/repo/beta/$arch\n"
    assert conf.read_text() == CONF.replace("Server = https://plinius.example/repo/$arch",
                                            "Server = https://plinius.example/repo/beta/$arch")
    # Twice beta stays beta
    assert run("beta").returncode == 0
    assert "beta/beta" not in conf.read_text()
    assert run("stable").returncode == 0
    assert conf.read_text() == CONF
    assert run("stable").returncode == 0
    assert conf.read_text() == CONF
    assert conf.stat().st_mode & 0o777 == 0o640
    assert not list(conf.parent.glob(".pacman.conf.*"))


def test_channel_rewrites_every_server_line_of_plinius_only(channel):
    conf, run = channel
    conf.write_text("[plinius]\n  Server=https://a.example/r/aarch64  \nServer = https://b.example/r/$arch\n"
                    "Include = /etc/pacman.d/mirrorlist\n[other]\nServer = https://c.example/x/$arch\n")
    assert run("beta").returncode == 0
    assert conf.read_text() == ("[plinius]\nServer = https://a.example/r/beta/aarch64\n"
                                "Server = https://b.example/r/beta/$arch\n"
                                "Include = /etc/pacman.d/mirrorlist\n[other]\nServer = https://c.example/x/$arch\n")


def test_channel_without_plinius_section_or_server_changes_nothing(channel):
    conf, run = channel
    conf.write_text("[core]\nServer = https://x/$arch\n")
    r = run("beta")
    assert r.returncode == 1 and "no repo [plinius]" in r.stderr
    conf.write_text("[plinius]\nSigLevel = Never\n[core]\nServer = https://x/$arch\n")
    r = run("beta")
    assert r.returncode == 1 and "no Server line" in r.stderr
    assert conf.read_text() == "[plinius]\nSigLevel = Never\n[core]\nServer = https://x/$arch\n"
    assert not list(conf.parent.glob(".pacman.conf.*"))


# --- Battery rule ------------------------------------------------------------------------------

@pytest.fixture
def battery(bash, tmp_path):
    supplies = tmp_path / "power_supply"
    supplies.mkdir()
    code = functions("battery_ok", replace={"/sys/class/power_supply": supplies})

    def supply(name, **values):
        d = supplies / name
        d.mkdir()
        for key, value in values.items():
            (d / key).write_text(f"{value}\n")

    def ok():
        return bash(code + "battery_ok").returncode == 0
    return supply, ok


def test_battery_limit_is_ten_percent():
    # The tests below set MIN_BATTERY themselves; the script itself must say 10
    assert re.search(r"^MIN_BATTERY=10$", SCRIPT.read_text(), re.M)


def test_battery_ok_without_any_supply(battery):
    _supply, ok = battery
    assert ok()


@pytest.mark.parametrize("supplies, expected", [
    ({"battery": {"type": "Battery", "capacity": 9, "status": "Discharging"}}, False),
    ({"battery": {"type": "Battery", "capacity": 10, "status": "Discharging"}}, True),
    ({"battery": {"type": "Battery", "capacity": 5, "status": "Charging"}}, True),
    ({"battery": {"type": "Battery", "capacity": 5, "status": "Discharging"},
      "ac": {"type": "Mains", "online": 1}}, True),
    ({"battery": {"type": "Battery", "capacity": 5, "status": "Discharging"},
      "usb": {"type": "USB", "online": 0}}, False),
    ({"battery": {"type": "Battery", "capacity": 5, "status": "Not charging"},
      "usb": {"type": "USB", "online": 1}}, True),
    ({"b1": {"type": "Battery", "capacity": 90, "status": "Discharging"},
      "b2": {"type": "Battery", "capacity": 9, "status": "Discharging"}}, False),
    ({"battery": {"type": "Battery", "status": "Discharging"}}, True),  # capacity unreadable
])
def test_battery_rule(battery, supplies, expected):
    supply, ok = battery
    for name, values in supplies.items():
        supply(name, **values)
    assert ok() is expected


# --- Lock -------------------------------------------------------------------------------------

def test_check_lock(bash, fake_bin, tmp_path):
    lock = tmp_path / "db.lck"
    code = functions("check_lock", replace={"/var/lib/pacman/db.lck": lock})
    fake_bin.stub("pgrep", 'exit "${PGREP_RC:-1}"')
    lock.write_text("")
    r = bash(code + "check_lock", PGREP_RC=0)
    assert r.returncode == 9 and lock.exists()  # a running pacman keeps its lock
    r = bash(code + "check_lock; echo go")
    assert r.returncode == 0 and r.stdout == "go\n" and not lock.exists()
    assert "interrupted operation" in r.stderr


# --- Install: download first, then install ----------------------------------------------------

PACMAN_INSTALL = """
[ "$1" = -Qqu ] && { printf '%s' "${QQU:-}"; exit 0; }
for a; do
	case "$a" in
		-Sw|-S) [ "$HTTP_USER_AGENT" = "Plinius Arch Linux" ] || exit 98 ;;
	esac
	case "$a" in
		-Sw) printf '%s\\n' "${SW_OUT:-}"; exit "${SW_RC:-0}" ;;
		-S) printf '%s\\n' "${S_OUT:-}"; exit "${S_RC:-0}" ;;
	esac
done
exit 0
"""


@pytest.fixture
def install(bash, fake_bin):
    fake_bin.stub("pacman", PACMAN_INSTALL)
    fake_bin.stub("systemd-inhibit", 'while [[ $1 == --* ]]; do shift; done; exec "$@"')
    code = functions("inhibit", "run", "held", "classify", "pacman_install")

    def run(*pkgs, **env):
        return bash(code + 'pacman_install "$@"', *pkgs, **env)
    return run


def test_install_downloads_before_installing(install, fake_bin):
    assert install("gnome-maps", "x").returncode == 0
    calls = fake_bin.calls("pacman")
    assert calls == [["-Qqu"],
                     ["--noconfirm", "--noprogressbar", "-Sw", "--needed", "gnome-maps", "x"],
                     ["--noconfirm", "--noprogressbar", "-S", "--needed", "gnome-maps", "x"]]
    inhibit = fake_bin.calls("systemd-inhibit")[0]
    assert "--what=sleep:shutdown" in inhibit


def test_install_refused_while_the_database_is_newer(install, fake_bin):
    r = install("gnome-maps", QQU="glibc\n")
    assert r.returncode == 3
    assert fake_bin.calls("pacman") == [["-Qqu"]]


def test_install_missing_file_on_the_server_leaves_the_system_alone(install, fake_bin):
    r = install("gnome-maps", SW_RC=1, SW_OUT="error: The requested URL returned error: 404")
    assert r.returncode == 4
    assert [c[2] for c in fake_bin.calls("pacman")[1:]] == ["-Sw"]


def test_install_held_by_approval(install):
    r = install("glibc", S_RC=1, S_OUT=":: plinius-freigabe-5-1 and glibc-2.45-1 are in conflict")
    assert r.returncode == 8 and "held back" in r.stderr
    # Also when the download run already stops there
    r = install("glibc", SW_RC=1, SW_OUT=":: glibc-2.45-1 and plinius-freigabe-5-1 are in conflict")
    assert r.returncode == 8


# --- Flatpak remotes ---------------------------------------------------------------------------

FLATPAK = """
case "$1" in
	list) printf '%s\\n' ${ORIGINS:-} ;;
	remotes) printf '%s\\n' flathub furihub ;;
esac
exit 0
"""


@pytest.mark.parametrize("origins, code, deleted", [("flathub", 5, False), ("furihub", 0, True),
                                                    ("", 0, True)])
def test_remote_disable_only_without_its_apps(bash, fake_bin, origins, code, deleted):
    fake_bin.stub("flatpak", FLATPAK)
    fake_bin.stub("systemd-inhibit", 'while [[ $1 == --* ]]; do shift; done; exec "$@"')
    r = bash(functions("inhibit", "flatpak_run", "remote_enabled", "remote_disable")
             + "remote_disable flathub", ORIGINS=origins)
    assert r.returncode == code
    assert (["remote-delete", "--system", "flathub"] in fake_bin.calls("flatpak")) is deleted
