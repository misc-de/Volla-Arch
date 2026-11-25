# plinius-software (pkgbuilds/plinius-software): the logic behind the Software app and the
# update check, without opening a window: which updates are held by plinius-freigabe and how
# that is reported, the checkupdates parsing, what the stamp check (plinius-repo-stamp, tested in
# test_apps_repo_stamp.py) tells the user, the notifications' bookkeeping, the restart offer and
# the progress parsing.
#
# Nothing reaches the system: checkupdates, pacman, flatpak and plinius-repo-stamp answers are
# canned (or a copy of the helper with its paths in tmp_path runs), every state and settings
# path points into tmp_path.

import builtins
import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import types

import pytest

from conftest import PKG, load_script

SCRIPT = PKG / "plinius-software" / "plinius-software"
STAMP_SCRIPT = PKG / "plinius-software" / "plinius-repo-stamp"
needs_vercmp = pytest.mark.skipif(shutil.which("vercmp") is None, reason="no vercmp (pacman)")


@pytest.fixture(scope="module")
def mod():
    pytest.importorskip("gi")
    try:
        return load_script(SCRIPT)
    except (ImportError, ValueError) as e:  # a GTK 4 / libadwaita / AppStream typelib missing
        pytest.skip(f"plinius-software cannot be imported here: {e}")


@pytest.fixture
def sw(mod, tmp_path, monkeypatch):
    """Paths into tmp_path, messages untranslated."""
    monkeypatch.setattr(mod, "_", lambda s: s)
    monkeypatch.setattr(mod, "ngettext", lambda s, p, n: s if n == 1 else p)
    # STAMP_CHECK: a program that is not there unless a test puts one
    for name in ("SETTINGS", "LAST_CHECK", "STAMP_STATE", "HELD_SINCE", "RATINGS", "RECOMMENDED",
                 "RESTART_RESULT", "STAMP_CHECK"):
        monkeypatch.setattr(mod, name, str(tmp_path / name.lower()))

    real_glib = mod.GLib

    class GLibProxy:
        """The real GLib, but the user directories inside tmp_path."""

        def get_user_runtime_dir(self):
            return str(tmp_path / "run")

        def get_user_state_dir(self):
            return str(tmp_path / "statedir")

        def __getattr__(self, name):
            return getattr(real_glib, name)
    monkeypatch.setattr(mod, "GLib", GLibProxy())
    return tmp_path


# --- Small parsers -----------------------------------------------------------------------------

def test_removal_takes_addons_but_not_what_others_need(mod):
    required = {"firefox": ["firefox-i18n-de"], "firefox-i18n-de": [],
                "gtk4": ["nautilus"], "foo": ["foo-bar"], "foo-bar": ["baz"]}
    assert mod.removal("firefox", required) == ["firefox", "firefox-i18n-de"]
    assert mod.removal("gtk4", required) is None
    assert mod.removal("foo", required) is None  # the add-on itself is needed by baz
    assert mod.removal("alone", required) == ["alone"]


QI = """\
Name            : glibc
Version         : 2.44-1
Description     : GNU C Library: the core
Provides        : None
Required By     : bash  coreutils
                  util-linux
Optional For    : None

Name            : gnome-control-center-plinius
Version         : 49.0-80
Provides        : gnome-control-center=49.0  gnome-settings
Required By     : None
"""


def test_package_info_reads_required_by_and_provides(mod, monkeypatch):
    monkeypatch.setattr(mod, "lines", lambda *cmd: QI.splitlines())
    required, provider = mod.package_info()
    assert required == {"glibc": ["bash", "coreutils", "util-linux"],
                        "gnome-control-center-plinius": []}
    assert provider == {"gnome-control-center": "gnome-control-center-plinius",
                        "gnome-settings": "gnome-control-center-plinius"}


def test_load_ratings_skips_invalid_lines(mod, sw):
    (sw / "ratings").write_text("# comment\norg.a phone\norg.b scaled With a note  \n"
                                "org.c fantastic\norg.d\n\norg.e desktop\n")
    assert mod.load_ratings() == {"org.a": ("phone", ""), "org.b": ("scaled", "With a note"),
                                  "org.e": ("desktop", "")}


def test_load_ratings_and_recommended_without_files(mod, sw):
    assert mod.load_ratings() == {}
    assert mod.load_recommended() == set()
    (sw / "recommended").write_text("# apps\nfirefox\n\n  gnome-maps  \n")
    assert mod.load_recommended() == {"firefox", "gnome-maps"}


def test_shown_settings_round_trip(mod, sw):
    assert mod.load_shown() == {"phone", "scaled"}
    mod.save_shown({"phone", "scaled", "unknown"})
    assert mod.load_shown() == {"phone", "scaled", "unknown"}
    mod.save_shown({"desktop"})
    # phone and scaled are always shown
    assert mod.load_shown() == {"phone", "scaled", "desktop"}


def test_in_category(mod):
    class App:
        recommended = True
        categories = {"Network", "Chat"}
    assert mod.in_category(App, None)
    assert mod.in_category(App, "recommended")
    assert mod.in_category(App, {"Network"})
    assert not mod.in_category(App, {"Game"})


def test_remote_title(mod):
    assert mod.remote_title("flathub") == "Flathub"
    assert mod.remote_title("furihub") == "FuriHub"
    assert mod.remote_title("other") == "other"
    assert mod.remote_title("") == "Flatpak"


# --- Approval limits (plinius-freigabe) ------------------------------------------------------

def freigabe(version, conflicts):
    return ["Name            : plinius-freigabe", f"Version         : {version}",
            f"Conflicts With  : {conflicts}", "Replaces        : None"]


@pytest.fixture
def approval(mod, monkeypatch):
    """Canned pacman answers: -Qi/-Si of plinius-freigabe and -Q of the limited packages."""
    setup = {"Qi": freigabe("5-1", "glibc>2.44-1  mesa>=26.0-1  kernel<6.1.190-1"),
             "Si": [], "installed": {"glibc": "2.44-1", "mesa": "25.9-1", "kernel": "6.1.192-1"}}

    def lines(*cmd):
        if "-Qi" in cmd:
            return setup["Qi"]
        if "-Si" in cmd:
            return setup["Si"]
        raise AssertionError(cmd)

    def pacman_lines(*args):
        assert args[0] == "-Q"
        return [f"{n} {setup['installed'][n]}" for n in args[1:] if n in setup["installed"]]
    monkeypatch.setattr(mod, "lines", lines)
    monkeypatch.setattr(mod, "pacman_lines", pacman_lines)
    return setup


@needs_vercmp
def test_approval_limits_parse_conflicts(mod, approval):
    assert sorted(mod.approval_limits("/db")) == [("glibc", ">", "2.44-1"),
                                                  ("kernel", "<", "6.1.190-1"),
                                                  ("mesa", ">=", "26.0-1")]


@needs_vercmp
def test_approval_limits_come_from_the_newer_freigabe(mod, approval):
    # After the -Syu the newer plinius-freigabe from the sync database applies
    approval["Si"] = freigabe("6-1", "None")
    assert mod.approval_limits("/db") == []
    approval["Si"] = freigabe("4-1", "None")
    assert len(mod.approval_limits("/db")) == 3


@needs_vercmp
def test_held_by_approval(mod, approval):
    # Nothing beyond its limit after the update
    assert mod.held_by_approval([("glibc", "2.43-1", "2.44-1")], "/db") == []
    # glibc would go beyond its approved version: (name, version after the update, operator, limit)
    assert mod.held_by_approval([("glibc", "2.44-1", "2.45-1")], "/db") == [("glibc", "2.45-1", ">", "2.44-1")]
    assert mod.held_by_approval([("mesa", "25.9-1", "26.0-1")], "/db") == [("mesa", "26.0-1", ">=", "26.0-1")]
    # Both, in the order of the limits
    assert mod.held_by_approval([("mesa", "25.9-1", "26.0-1"), ("glibc", "2.44-1", "2.45-1")], "/db") == [
        ("glibc", "2.45-1", ">", "2.44-1"), ("mesa", "26.0-1", ">=", "26.0-1")]
    # Already beyond its limit without any update (a new approval constrains it)
    approval["installed"]["mesa"] = "26.1-1"
    assert mod.held_by_approval([], "/db") == [("mesa", "26.1-1", ">=", "26.0-1")]


@needs_vercmp
def test_held_by_approval_lower_limit_and_missing_packages(mod, approval):
    approval["Qi"] = freigabe("5-1", "kernel<6.1.190-1  notinstalled>1")
    assert mod.held_by_approval([], "/db") == []
    assert mod.held_by_approval([("kernel", "6.1.192-1", "6.1.180-1")], "/db") == [
        ("kernel", "6.1.180-1", "<", "6.1.190-1")]
    approval["Qi"] = freigabe("5-1", "None")
    assert mod.held_by_approval([("glibc", "1", "99")], "/db") == []
    approval["Qi"] = []  # plinius-freigabe not installed at all
    assert mod.held_by_approval([("glibc", "1", "99")], "/db") == []


@needs_vercmp
@pytest.mark.parametrize("conflict, version, held", [
    ("glibc=2.45-1", "2.45-1", True), ("glibc=2.45-1", "2.45-2", False),
    ("glibc<=2.45-1", "2.45-1", True), ("glibc<=2.45-1", "2.46-1", False),
    ("glibc>=2.45-1", "2.44-9", False),
])
def test_held_by_approval_operators(mod, approval, conflict, version, held):
    approval["Qi"] = freigabe("5-1", conflict)
    assert bool(mod.held_by_approval([("glibc", "2.40-1", version)], "/db")) is held


# --- When a hold started ---------------------------------------------------------------------

@pytest.fixture
def clock(mod, monkeypatch):
    """time for plinius-software: `now` (seconds) and the sleeps it asked for."""
    setup = {"now": 1_800_000_000, "slept": []}
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(time=lambda: setup["now"],
                                                           sleep=setup["slept"].append))
    return setup


def test_held_since_stays_while_the_hold_lasts(mod, sw, clock):
    assert mod.held_since([]) is None and not os.path.exists(mod.HELD_SINCE)
    assert mod.held_since([("glibc", "2.45-1", ">", "2.44-1")]) == 1_800_000_000
    clock["now"] += 86400
    # Other packages, the same hold: the time it started stays
    assert mod.held_since([("mesa", "26.0-1", ">=", "26.0-1")]) == 1_800_000_000
    # Over: forgotten; the next hold starts anew
    assert mod.held_since([]) is None and not os.path.exists(mod.HELD_SINCE)
    assert mod.held_since([("mesa", "26.0-1", ">=", "26.0-1")]) == 1_800_086_400


def test_held_since_unreadable_file_starts_anew(mod, sw, clock):
    with builtins.open(mod.HELD_SINCE, "w") as f:
        f.write("yesterday")
    assert mod.held_since([("glibc", "2.45-1", ">", "2.44-1")]) == 1_800_000_000
    with builtins.open(mod.HELD_SINCE) as f:
        assert f.read() == "1800000000"


# --- find_updates ------------------------------------------------------------------------------

@pytest.fixture
def checkupdates(mod, sw, monkeypatch):
    """checkupdates answered from `result`; every other program must not run."""
    result = {"rc": 0, "out": "", "env": None}

    def run(cmd, **kwargs):
        if cmd == ["checkupdates"]:
            result["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(cmd, result["rc"], result["out"], "")
        raise AssertionError(f"unexpected command {cmd}")
    monkeypatch.setattr(mod.subprocess, "run", run)
    monkeypatch.setattr(mod, "held_by_approval", lambda packages, db: [])
    monkeypatch.setattr(mod, "flathub_enabled", lambda: False)
    return result


def test_find_updates_parses_checkupdates(mod, sw, checkupdates):
    checkupdates["out"] = "glibc 2.44-1 -> 2.45-1\nmesa 26.0-1 -> 26.0-2 [ignored]\njunk\n"
    code, packages, flatpaks, hold = mod.find_updates()
    assert code == 0 and flatpaks == [] and hold is None
    assert packages == [("glibc", "2.44-1", "2.45-1"), ("mesa", "26.0-1", "26.0-2")]
    # checkupdates works on its own database copy in the user's runtime directory
    assert checkupdates["env"]["CHECKUPDATES_DB"] == str(sw / "run" / "plinius-software" / "db")
    assert os.path.exists(mod.LAST_CHECK)


def test_find_updates_failure_lists_nothing_and_is_no_check(mod, sw, checkupdates, monkeypatch):
    checkupdates.update(rc=1, out="glibc 2.44-1 -> 2.45-1\n")
    monkeypatch.setattr(mod, "held_by_approval", lambda packages, db: pytest.fail("no approval check"))
    assert mod.find_updates() == (1, [], [], None)
    assert not os.path.exists(mod.LAST_CHECK)
    monkeypatch.setattr(mod, "held_by_approval", lambda packages, db: [])
    checkupdates.update(rc=2, out="")
    assert mod.find_updates() == (2, [], [], None)
    assert os.path.exists(mod.LAST_CHECK)


def test_find_updates_held_update_lists_no_package_but_says_why(mod, sw, checkupdates, clock, monkeypatch):
    checkupdates["out"] = "glibc 2.44-1 -> 2.45-1\nzlib 1-1 -> 1-2\n"
    limits = [("glibc", "2.45-1", ">", "2.44-1")]
    asked = []
    monkeypatch.setattr(mod, "held_by_approval", lambda packages, db: asked.append((packages, db)) or limits)
    waiting = [("glibc", "2.44-1", "2.45-1"), ("zlib", "1-1", "1-2")]
    assert mod.find_updates() == (0, [], [], {"limits": limits, "packages": waiting, "since": 1_800_000_000})
    # Asked about the updates on checkupdates' database copy
    assert asked == [(waiting, str(sw / "run" / "plinius-software" / "db"))]
    clock["now"] += 3600
    assert mod.find_updates()[3]["since"] == 1_800_000_000
    # The hold is over: the updates are offered again, its start is forgotten
    monkeypatch.setattr(mod, "held_by_approval", lambda packages, db: [])
    assert mod.find_updates() == (0, waiting, [], None)
    assert not os.path.exists(mod.HELD_SINCE)


def test_find_updates_without_checkupdates(mod, sw, monkeypatch):
    def run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(mod.subprocess, "run", run)
    monkeypatch.setattr(mod, "flathub_enabled", lambda: False)
    assert mod.find_updates() == (1, [], [], None)


def test_find_updates_lists_flatpak_updates(mod, sw, checkupdates, monkeypatch):
    checkupdates["rc"] = 2
    monkeypatch.setattr(mod, "flathub_enabled", lambda: True)
    monkeypatch.setattr(mod, "lines", lambda *cmd: ["org.a\t1.2\tflathub", "org.b\t\tfurihub",
                                                    "org.c"])
    assert mod.find_updates()[2] == [("org.a", "1.2", "flathub"), ("org.b", "", "furihub"),
                                     ("org.c", "", "")]


def test_find_updates_asks_flathub_while_checkupdates_runs(mod, sw, monkeypatch):
    """checkupdates only returns once Flathub has been asked: run one after the other, the
    fake checkupdates would wait in vain."""
    asked = threading.Event()

    def run(cmd, **kwargs):
        assert cmd == ["checkupdates"]
        assert asked.wait(5), "Flathub was not asked while checkupdates ran"
        return subprocess.CompletedProcess(cmd, 2, "", "")

    def lines(*cmd):
        assert cmd[:3] == ("flatpak", "remote-ls", "--updates")
        asked.set()
        return ["org.a\t1.2\tflathub"]
    monkeypatch.setattr(mod.subprocess, "run", run)
    monkeypatch.setattr(mod, "held_by_approval", lambda packages, db: [])
    monkeypatch.setattr(mod, "flathub_enabled", lambda: True)
    monkeypatch.setattr(mod, "lines", lines)
    assert mod.find_updates() == (2, [], [("org.a", "1.2", "flathub")], None)


# --- Holds by plinius-freigabe, as the user sees them ----------------------------------------

HOLD = {"limits": [("glibc", "2.45-1", ">", "2.44-1"), ("mesa", "26.0-1", ">=", "26.0-1")],
        "packages": [("glibc", "2.44-1", "2.45-1"), ("mesa", "25.9-1", "26.0-1"), ("zlib", "1-1", "1-2")],
        "since": None}
HELD_TITLE = "A system update is being tested"
HELD_TEXT = ("New versions from Arch Linux are first tested with the Plinius adaptations and installed once "
             "they work together. You do not need to do anything.")


def test_hold_texts(mod, sw):
    # For the user, who can do nothing about it: no package names, no count of packages
    assert mod.hold_texts(HOLD) == (HELD_TITLE, HELD_TEXT)
    since = int(time.time())
    one = {"limits": HOLD["limits"][:1], "packages": HOLD["packages"][:1], "since": since}
    title, text = mod.hold_texts(one)
    assert title == HELD_TITLE
    assert text.split("\n") == [HELD_TEXT, "Waiting since today at " + time.strftime("%H:%M", time.localtime(since))]
    assert "glibc" not in title + text


def test_hold_texts_name_security_updates_after_a_week(mod, sw, clock):
    six = dict(HOLD, since=clock["now"] - 6 * 86400 - 60)
    assert mod.hold_texts(six)[1].split("\n")[0] == HELD_TEXT
    week = dict(HOLD, since=clock["now"] - 7 * 86400)
    assert mod.hold_texts(week)[1].split("\n")[0] == HELD_TEXT + " Security updates are waiting too."


def test_hold_days(mod, sw, clock):
    assert mod.hold_days(HOLD) == 0
    assert mod.hold_days(dict(HOLD, since=clock["now"] - 3 * 86400 - 1)) == 3
    assert mod.hold_days(dict(HOLD, since=clock["now"] + 86400)) == 0


def test_hold_json(mod, sw, clock):
    assert mod.hold_json(None) is None
    data = mod.hold_json(HOLD)
    assert data == {"limits": HOLD["limits"], "packages": HOLD["packages"], "since": None, "days": 0,
                    "title": HELD_TITLE, "text": HELD_TEXT}
    held = dict(HOLD, since=1_800_000_000 - 8 * 86400 - 60)
    assert mod.hold_json(held)["days"] == 8
    assert mod.hold_json(held)["text"].startswith(HELD_TEXT + " Security updates are waiting too.\nWaiting since on ")
    # A start in the future (clock set back) is no negative wait
    assert mod.hold_json(dict(HOLD, since=1_800_000_000 + 86400))["days"] == 0
    # As Settings reads it: limits and packages become lists
    assert json.loads(json.dumps(data))["limits"][0] == ["glibc", "2.45-1", ">", "2.44-1"]


# --- Freshness stamp: what plinius-repo-stamp says, as the user sees it -----------------------

def test_check_repo_stamp_asks_the_helper_about_checkupdates_database(mod, sw, monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        # Warnings come with an exit code other than 0; the verdict is read all the same
        return subprocess.CompletedProcess(cmd, 3, '{"result": "mismatch", "channel": "stable"}\n', "")
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod.check_repo_stamp() == {"result": "mismatch", "channel": "stable"}
    (cmd, kwargs), = calls
    assert cmd == [mod.STAMP_CHECK, "check", "--db",
                   str(sw / "run" / "plinius-software" / "db" / "sync" / "plinius.db"),
                   "--user-state", mod.STAMP_STATE]
    assert kwargs["capture_output"] and kwargs["timeout"] >= 2 * 30  # two fetches with their timeout


@pytest.mark.parametrize("answer", [FileNotFoundError("plinius-repo-stamp"),
                                    subprocess.TimeoutExpired("plinius-repo-stamp", 180),
                                    "", "usage: ...", "[1, 2]", '"ok"'])
def test_check_repo_stamp_without_a_verdict(mod, sw, monkeypatch, answer):
    def run(cmd, **kwargs):
        if isinstance(answer, BaseException):
            raise answer
        return subprocess.CompletedProcess(cmd, 1, answer, "")
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod.check_repo_stamp() is None


def test_check_repo_stamp_without_the_helper_installed(mod, sw):
    assert not os.path.exists(mod.STAMP_CHECK)
    assert mod.check_repo_stamp() is None


def test_check_repo_stamp_with_the_real_helper(mod, sw, monkeypatch):
    # A copy of plinius-repo-stamp with its paths in tmp_path; [plinius] has no https server, so
    # it answers without the network
    text = STAMP_SCRIPT.read_text()
    for name in ("/etc/pacman.conf", "/usr/share/pacman/keyrings/plinius.gpg",
                 "/usr/share/pacman/keyrings/plinius-trusted", "/usr/share/pacman/keyrings/plinius-revoked",
                 "/var/lib/plinius-pkg/repo-stamp"):
        assert f'"{name}"' in text
        text = text.replace(f'"{name}"', repr(str(sw / "system" / name.strip("/").replace("/", "_"))))
    text = text.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1)
    helper = sw / "plinius-repo-stamp"
    helper.write_text(text)
    helper.chmod(0o755)
    (sw / "system").mkdir()
    (sw / "system" / "etc_pacman.conf").write_text("[plinius]\nServer = file:///srv/repo/$arch\n")
    monkeypatch.setattr(mod, "STAMP_CHECK", str(helper))
    verdict = mod.check_repo_stamp()
    assert verdict == {"result": "skip", "message": "no https server for [plinius], nothing to check"}
    assert mod.stamp_warning(verdict) is None


@pytest.mark.parametrize("verdict, warning", [
    (None, "The update server gives no proof of how current its updates are."),
    ({}, "The update server gives no proof of how current its updates are."),
    ({"result": "ok"}, None),
    ({"result": "skip"}, None),
    ({"result": "none"}, None),
    ({"result": "unreachable"}, "The update server did not hand out its signed state. Updates may be held back."),
    ({"result": "missing"}, "The update server gives no proof of how current its updates are."),
    ({"result": "http", "status": 500}, "The update server gives no proof of how current its updates are."),
    ({"result": "malformed"}, "The update server gives no proof of how current its updates are."),
    ({"result": "unsigned"}, "The update server's state is not signed by Plinius. Updates are not trustworthy."),
    ({"result": "channel", "got": "stable", "want": "beta"},
     "The update server offers the updates of another channel (stable instead of beta)."),
    ({"result": "replay"}, "The update server offers an older state than before. Updates may be held back."),
    ({"result": "unhashed"},
     "The update server's signed state does not vouch for its package list. Updates may be held back."),
    ({"result": "mismatch"}, "The update server's package list does not match its signed state. Updates may "
                             "be held back or come from another channel."),
    ({"result": "unreadable", "db": "/x", "error": "Permission denied"},
     "The package list could not be read to check it against the update server's signed state."),
    ({"result": "stale", "days": 1}, "The update server's state is 1 day old. Updates may be held back."),
    ({"result": "stale", "days": 31}, "The update server's state is 31 days old. Updates may be held back."),
])
def test_stamp_warning(mod, sw, verdict, warning):
    assert mod.stamp_warning(verdict) == warning


def test_every_verdict_of_the_helper_is_known(mod, sw):
    helper = load_script(STAMP_SCRIPT, "repo_stamp_for_software")
    generic = mod.stamp_warning({"result": "malformed"})
    for result in helper.MESSAGES:
        # Only the three results without a word of their own share the generic warning
        assert (mod.stamp_warning({"result": result, "days": 31}) == generic) is (
            result in ("missing", "http", "malformed")), result


@pytest.fixture
def checks(mod, sw, clock, monkeypatch):
    """check_all with canned rounds: find_updates and check_repo_stamp answer from the lists."""
    setup = {"updates": [], "verdicts": [], "found": 0, "asked": 0}

    def find_updates():
        setup["found"] += 1
        return setup["updates"].pop(0)

    def check_repo_stamp():
        setup["asked"] += 1
        return setup["verdicts"].pop(0)
    monkeypatch.setattr(mod, "find_updates", find_updates)
    monkeypatch.setattr(mod, "check_repo_stamp", check_repo_stamp)
    setup["slept"] = clock["slept"]
    return setup


UPDATES = (0, [("glibc", "1", "2")], [], None)


def test_check_all_failed_check_asks_for_no_stamp(mod, checks):
    checks["updates"] = [(1, [], [], None)]
    assert mod.check_all() == (1, [], [], None, None)
    assert checks["asked"] == 0


def test_check_all_good_stamp(mod, checks):
    checks.update(updates=[UPDATES], verdicts=[{"result": "ok"}])
    assert mod.check_all() == (*UPDATES, None)
    assert checks["slept"] == []


def test_check_all_warning_without_a_second_round(mod, checks):
    checks.update(updates=[UPDATES], verdicts=[{"result": "replay"}])
    assert mod.check_all()[4] == mod.stamp_warning({"result": "replay"})
    assert (checks["found"], checks["asked"], checks["slept"]) == (1, 1, [])


def test_check_all_mismatch_fetches_both_again(mod, checks):
    # An upload switching database and stamp over: the second round sees both new
    second = (0, [("glibc", "1", "3")], [], None)
    checks.update(updates=[UPDATES, second], verdicts=[{"result": "mismatch"}, {"result": "ok"}])
    assert mod.check_all() == (*second, None)
    assert checks["slept"] == [mod.STAMP_RETRY_DELAY]
    checks.update(updates=[UPDATES, UPDATES], verdicts=[{"result": "mismatch"}, {"result": "mismatch"}])
    assert mod.check_all()[4] == mod.stamp_warning({"result": "mismatch"})
    assert (checks["found"], checks["asked"]) == (4, 4)


def test_check_all_mismatch_then_a_failed_check(mod, checks):
    checks.update(updates=[UPDATES, (1, [], [], None)], verdicts=[{"result": "mismatch"}])
    assert mod.check_all() == (1, [], [], None, None)
    assert checks["asked"] == 1


# --- Results for Settings and the timer ------------------------------------------------------

def test_updates_json(mod, sw, checks, monkeypatch, capsys):
    monkeypatch.setattr(mod, "last_check_text", lambda: "today at 10:00")
    checks.update(updates=[(0, [("glibc", "1", "2")], [("org.a", "1", "flathub")], None)],
                  verdicts=[{"result": "replay"}])
    assert mod.updates_json() == 0
    assert json.loads(capsys.readouterr().out) == {
        "code": 0, "packages": [["glibc", "1", "2"]], "flatpaks": [["org.a", "1", "flathub"]],
        "last_check": "today at 10:00", "repo_warning": mod.stamp_warning({"result": "replay"}),
        "held": None}
    # A failed check does not ask the server for its stamp
    checks["updates"] = [(1, [], [], None)]
    mod.updates_json()
    assert json.loads(capsys.readouterr().out)["repo_warning"] is None
    assert checks["asked"] == 1


def test_updates_json_names_the_hold(mod, sw, checks, monkeypatch, capsys):
    monkeypatch.setattr(mod, "last_check_text", lambda: None)
    hold = dict(HOLD, since=1_800_000_000 - 2 * 86400)
    checks.update(updates=[(0, [], [], hold)], verdicts=[{"result": "ok"}])
    mod.updates_json()
    out = json.loads(capsys.readouterr().out)
    assert out["packages"] == [] and out["repo_warning"] is None
    assert out["held"]["title"] == HELD_TITLE and out["held"]["days"] == 2
    assert out["held"]["packages"] == [list(p) for p in HOLD["packages"]]
    assert out["held"]["limits"] == [list(limit) for limit in HOLD["limits"]]
    assert out["held"]["since"] == 1_800_000_000 - 2 * 86400


@pytest.fixture
def timer(mod, sw, clock, monkeypatch):
    sent = []
    setup = {"result": (0, [], [], None), "verdict": {"result": "ok"}, "sent": sent}
    # The real find_updates makes this directory (touch_last_check) before any notification
    os.makedirs(sw / "statedir" / "plinius-software")
    monkeypatch.setattr(mod, "find_updates", lambda: setup["result"])
    monkeypatch.setattr(mod, "check_repo_stamp", lambda: setup["verdict"])
    monkeypatch.setattr(mod, "send_update_notification", lambda summary, body: sent.append((summary, body)))
    return setup


def test_notify_updates_once_per_set(mod, timer):
    timer["verdict"] = {"result": "ok"}
    timer["result"] = (0, [("glibc", "1", "2")], [("org.a", "3", "flathub")], None)
    mod.notify_updates()
    assert timer["sent"] == [("System update available", "{n} packages can be updated.".format(n=2))]
    mod.notify_updates()
    assert len(timer["sent"]) == 1
    timer["result"] = (0, [("glibc", "1", "3")], [], None)
    mod.notify_updates()
    assert timer["sent"][-1][1] == "1 package can be updated."
    # Nothing left: forgotten, so the same update later notifies again
    timer["result"] = (2, [], [], None)
    mod.notify_updates()
    timer["result"] = (0, [("glibc", "1", "3")], [], None)
    mod.notify_updates()
    assert len(timer["sent"]) == 3


def test_notify_updates_failed_check_is_silent(mod, timer, monkeypatch):
    timer["result"] = (1, [("glibc", "1", "2")], [], None)
    monkeypatch.setattr(mod, "check_repo_stamp", lambda: pytest.fail("no stamp check"))
    assert mod.notify_updates() == 0
    assert timer["sent"] == []


def test_repo_warning_notified_once_per_text(mod, timer):
    replay, unsigned = (mod.stamp_warning({"result": r}) for r in ("replay", "unsigned"))
    timer["verdict"] = {"result": "replay"}
    mod.notify_updates()
    mod.notify_updates()
    assert timer["sent"] == [("Updates could not be verified", replay)]
    timer["verdict"] = {"result": "unsigned"}
    mod.notify_updates()
    timer["verdict"] = {"result": "ok"}
    mod.notify_updates()
    timer["verdict"] = {"result": "unsigned"}
    mod.notify_updates()
    assert [b for _s, b in timer["sent"]] == [replay, unsigned, unsigned]


def test_hold_notified_only_after_a_week(mod, sw, timer, clock):
    # A hold that has just started is no news for the user: nothing to do, usually over soon
    since = clock["now"]
    timer["result"] = (0, [], [], dict(HOLD, since=since))
    mod.notify_updates()
    clock["now"] += 6 * 86400
    mod.notify_updates()
    assert timer["sent"] == []
    # A week on: once, with what waits; then quiet
    clock["now"] += 86400
    mod.notify_updates()
    assert timer["sent"] == [("System updates are on hold",
                              "System updates have been on hold for 7 days. Security updates wait too.")]
    assert "glibc" not in timer["sent"][0][1]
    clock["now"] += 3 * 86400
    mod.notify_updates()
    assert len(timer["sent"]) == 1
    notified = sw / "statedir" / "plinius-software" / "held-notified"
    assert notified.read_text() == "long"


def test_hold_notified_again_after_it_ended(mod, sw, timer, clock):
    notified = sw / "statedir" / "plinius-software" / "held-notified"
    old = dict(HOLD, since=clock["now"] - 8 * 86400)
    timer["result"] = (0, [], [], old)
    mod.notify_updates()
    # Other packages, the same hold: no second notification
    timer["result"] = (0, [], [], dict(old, limits=[("mesa", "26.0-1", ">=", "26.0-1")]))
    mod.notify_updates()
    assert len(timer["sent"]) == 1
    # Over: forgotten; a new hold is quiet until it has lasted a week
    timer["result"] = (2, [], [], None)
    mod.notify_updates()
    assert not notified.exists()
    timer["result"] = (0, [], [], dict(HOLD, since=clock["now"]))
    mod.notify_updates()
    assert len(timer["sent"]) == 1
    timer["result"] = (0, [], [], dict(HOLD, since=clock["now"] - 7 * 86400))
    mod.notify_updates()
    assert len(timer["sent"]) == 2


def test_hold_and_update_notifications_are_separate(mod, timer, clock):
    # Flatpak apps still update while the system packages are on hold
    timer["result"] = (0, [], [("org.a", "3", "flathub")], dict(HOLD, since=clock["now"] - 7 * 86400))
    timer["verdict"] = {"result": "stale", "days": 40}
    mod.notify_updates()
    assert [s for s, _b in timer["sent"]] == ["Updates could not be verified", "System updates are on hold",
                                              "System update available"]
    assert timer["sent"][2][1] == "1 package can be updated."


# --- The Updates tab of the app ----------------------------------------------------------------

class Recorder:
    """Stands in for a widget: records the calls of its setters."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, *args))

    def last(self, name):
        return [c[1:] for c in self.calls if c[0] == name][-1]


@pytest.fixture
def updates_tab(mod, sw, monkeypatch):
    monkeypatch.setattr(mod, "plain_row", lambda title, subtitle=None: ("row", title, subtitle))
    monkeypatch.setattr(mod, "last_check_text", lambda: "today at 10:00")
    win = types.SimpleNamespace(check_button=Recorder(), update_list=Recorder(), update_status=Recorder(),
                                upgrade_button=Recorder(), updates_page=Recorder(), update_rows=[])

    def show(code, packages, flatpaks, hold):
        mod.Window.on_updates(win, code, packages, flatpaks, hold)
        return win
    return show


def test_updates_tab_names_a_hold_instead_of_up_to_date(mod, updates_tab):
    win = updates_tab(0, [], [], HOLD)
    assert win.update_status.last("set_title") == (HELD_TITLE,)
    assert win.update_status.last("set_description") == (HELD_TEXT,)
    assert win.update_list.last("set_visible") == (False,) and win.upgrade_button.last("set_visible") == (False,)
    assert win.updates_page.last("set_badge_number") == (0,)
    win = updates_tab(2, [], [], None)
    assert win.update_status.last("set_title") == ("The system is up to date",)


def test_updates_tab_hold_with_flatpak_updates(mod, updates_tab):
    win = updates_tab(0, [], [("org.a", "3", "flathub")], HOLD)
    assert win.update_status.last("set_title") == (HELD_TITLE,)
    # The Flatpak update is listed and can be installed
    assert win.update_rows == [("row", "org.a", "Flathub 3")]
    assert win.upgrade_button.last("set_visible") == (True,)
    assert win.updates_page.last("set_badge_number") == (1,)


def test_upgrade_that_could_not_be_verified(mod, sw):
    started = []
    win = types.SimpleNamespace(
        flathub=types.SimpleNamespace(get_state=lambda: types.SimpleNamespace(get_boolean=lambda: False)),
        run_helper=lambda args, title, steps, done: started.append((args, steps, done)),
        check_updates=lambda: started.append("check"), common_failure=lambda code, page: False)
    mod.Window.upgrade(win)
    (args, steps, done), = started
    assert args == ["upgrade"] and mod.FLATPAK_UPDATE not in steps
    page = Page()
    assert not done(mod.REPO_UNVERIFIED, page)
    assert page.finished == (False, "Updates could not be verified",
                             "The update server's package list does not match its signed state. "
                             "Nothing was changed. Try again later.")
    assert started[-1] == "check"  # the Updates tab is checked again
    done(1, page)
    assert page.finished[1] == "Update failed"


# --- Last check --------------------------------------------------------------------------------

def test_last_check_text(mod, sw):
    assert mod.last_check_text() is None
    mod.touch_last_check()
    mtime = time.localtime(os.path.getmtime(mod.LAST_CHECK))
    assert mod.last_check_text() == "today at " + time.strftime("%H:%M", mtime)
    noon = datetime.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    if noon.timetuple().tm_yday > 1:
        yesterday = (noon - datetime.timedelta(days=1)).timestamp()
        os.utime(mod.LAST_CHECK, (yesterday, yesterday))
        assert mod.last_check_text() == "yesterday at 12:00"
    old = (noon - datetime.timedelta(days=400)).timestamp()
    os.utime(mod.LAST_CHECK, (old, old))
    assert mod.last_check_text().startswith("on ")


# --- Restart offer after an update ----------------------------------------------------------

class Page:
    def __init__(self):
        self.finished, self.buttons = None, []

    def finish(self, ok, title, text=""):
        self.finished = (ok, title, text)

    def add_button(self, label, callback, style=None):
        self.buttons.append((label, callback))


@pytest.fixture
def restart(mod, sw, monkeypatch):
    applied = []
    monkeypatch.setattr(mod, "desktop_info", lambda desktop_id: None)
    monkeypatch.setattr(mod, "restart_apply", applied.append)

    def offer(result):
        if result is not None:
            with builtins.open(mod.RESTART_RESULT, "w") as f:
                f.write(result if isinstance(result, str) else json.dumps(result))
        page = Page()
        return mod.offer_restart(page, "Done"), page, applied
    return offer


def app(id_, uid=None):
    return {"id": id_, "unit": f"app-gnome-{id_}-1.scope", "uid": os.getuid() if uid is None else uid}


@pytest.mark.parametrize("result", [None, "not json", {}, {"level": "reboot"}])
def test_offer_restart_defaults_to_a_reboot(mod, restart, result):
    added, page, _applied = restart(result)
    assert added is True
    assert [label for label, _cb in page.buttons] == ["Restart Phone"]
    assert page.buttons[0][1] is mod.reboot


def test_offer_restart_nothing_needed(mod, restart):
    for result in ({"level": "none"}, {"level": "apps", "apps": [app("org.a", uid=os.getuid() + 1)]}):
        added, page, _applied = restart(result)
        assert added is False and page.buttons == []
        assert "already uses the new versions" in page.finished[2]


def test_offer_restart_apps(mod, restart):
    added, page, applied = restart({"level": "apps", "apps": [app("org.b"), app("org.a"), app("org.a"),
                                                              {"uid": os.getuid()}]})
    assert added is True
    assert "org.b, org.a" in page.finished[2]
    assert [label for label, _cb in page.buttons] == ["Restart Apps"]
    page.buttons[0][1]()
    assert applied == ["apps"]


def test_offer_restart_ui(mod, restart):
    added, page, applied = restart({"level": "ui", "apps": [app("org.a")]})
    assert [label for label, _cb in page.buttons] == ["Restart Phosh"]
    assert "org.a also restart" in page.finished[2]
    page.buttons[0][1]()
    assert applied == ["ui"]


# --- Progress of a plinius-pkg operation -----------------------------------------------------

UPGRADE = """\
:: Synchronizing package databases...
 core downloading...
Packages (2) glibc-2.45-1  zlib-1.3-2
:: Retrieving packages...
 glibc-2.45-1-aarch64 downloading...
 zlib-1.3-2-aarch64 downloading...
checking keyring...
checking package integrity...
:: Processing package changes...
upgrading glibc...
upgrading zlib...
:: Running post-transaction hooks...
(1/4) Arming ConditionNeedsUpdate...
(4/4) Updating the info directory file...
::plinius:: flatpak
""".splitlines()


def test_progress_follows_an_upgrade(mod):
    steps = [mod.SYNC, mod.DOWNLOAD, mod.CHECK, mod.INSTALL, mod.HOOKS, mod.FLATPAK_UPDATE]
    p = mod.Progress(steps)
    assert p.fraction() == 0 and p.key() is None
    fractions, seen_counts = [], {}
    for line in UPGRADE:
        p.feed(line)
        fractions.append(p.fraction())
        seen_counts[p.key()] = p.count()
    assert fractions == sorted(fractions) and 0 < fractions[-1] < 1
    assert seen_counts["download"] == (2, 2)
    assert seen_counts["install"] == (2, 2)
    assert seen_counts["hooks"] == (4, 4)
    assert p.key() == "flatpak"


def test_progress_skipped_steps_count_as_done_and_never_go_back(mod):
    p = mod.Progress([mod.SYNC, mod.DOWNLOAD, mod.CHECK, mod.INSTALL, mod.HOOKS])
    p.feed(":: Synchronizing package databases...")
    p.feed("checking keyring...")  # nothing to download
    assert p.key() == "check"
    assert p.fraction() == pytest.approx((1 + 4) / 11)
    p.feed(":: Retrieving packages...")
    p.feed("::plinius:: sync")
    assert p.key() == "check"
    p.feed(":: Running post-transaction hooks...")
    p.feed("(9/4) more hooks than announced")
    assert p.fraction() == 1


def test_progress_ignores_counters_of_other_steps(mod):
    p = mod.Progress([mod.SYNC, mod.REMOVE, mod.HOOKS])
    p.feed("Packages (3) a b c")
    p.feed("removing a...")  # before the install section: not counted
    assert p.count() == (0, 0)
    p.feed(":: Processing package changes...")
    p.feed("removing a...")
    p.feed("removing b-with-dash...")
    p.feed("removing c... done")  # not pacman's format
    assert p.count() == (2, 3)


# --- The catalog: AppStream data as the lists show it --------------------------------------------
#
# Components are real AppStream objects, parsed from catalog XML like the one in
# /usr/share/swcatalog; only the pool that would read the system catalog is replaced.

def components(mod, *bodies):
    """AppStream components from <component> bodies (desktop applications)."""
    xml = ('<?xml version="1.0"?><components version="1.0" origin="test">'
           + "".join(f'<component type="desktop-application">{b}</component>' for b in bodies)
           + "</components>")
    md = mod.AppStream.Metadata()
    md.set_format_style(mod.AppStream.FormatStyle.CATALOG)
    md.parse_data(xml, -1, mod.AppStream.FormatKind.XML)
    box = md.get_components()
    return [box.index_safe(i) for i in range(box.get_size())]


@pytest.mark.parametrize("relations, rating", [
    # The smallest width on the shorter side decides
    ('<requires><display_length compare="ge">360</display_length></requires>', "phone"),
    ('<recommends><display_length compare="ge">768</display_length></recommends>'
     '<supports><display_length compare="ge">300</display_length></supports>', "phone"),
    ('<requires><display_length compare="ge">768</display_length></requires>', "desktop"),
    # "more than 360" does not fit 360
    ('<requires><display_length compare="gt">360</display_length></requires>', "desktop"),
    ('<requires><display_length compare="gt">359</display_length></requires>', "phone"),
    # The longer side, "at most", zero and other relations say nothing about the width
    ('<requires><display_length compare="ge" side="longest">1000</display_length>'
     '<display_length compare="le">300</display_length><display_length compare="ge">0</display_length>'
     '<control>touch</control></requires>', "unknown"),
    # Purism's hint only counts without a display_length
    ('<custom><value key="Purism::form_factor">mobile</value></custom>', "phone"),
    ('<custom><value key="Purism::form_factor">workstation</value></custom>', "unknown"),
    ('<requires><display_length compare="ge">1024</display_length></requires>'
     '<custom><value key="Purism::form_factor">mobile</value></custom>', "desktop"),
])
def test_classify_by_the_apps_own_display_length(mod, relations, rating):
    cpt, = components(mod, f"<id>org.a</id><name>A</name>{relations}")
    assert mod.classify(cpt) == rating


def test_icons_from_the_catalog(mod):
    cpt, = components(mod, "<id>org.a</id><name>A</name><icon type='stock'>org.a</icon>"
                           "<icon type='cached' width='64' height='64'>/cat/64/a.png</icon>"
                           "<icon type='cached' width='128' height='128'>/cat/128/a.png</icon>"
                           "<icon type='remote' width='100' height='100'>https://x/a.png</icon>")
    assert mod.icon_name(cpt) == "org.a"
    assert mod.icon_file(cpt, 64) == "/cat/64/a.png"
    assert mod.icon_file(cpt, 128) == "/cat/128/a.png"
    assert mod.icon_file(cpt, 100) == "/cat/128/a.png"  # remote icons are not loaded
    bare, = components(mod, "<id>org.b</id><name>B</name><icon type='remote'>https://x/b.png</icon>")
    assert mod.icon_name(bare) is None and mod.icon_file(bare, 64) is None


def test_screenshot_urls_one_image_per_screenshot(mod):
    cpt, = components(mod, """<id>org.a</id><name>A</name><screenshots>
      <screenshot type="default">
        <image type="source">https://s/1.png</image>
        <image type="thumbnail" width="1248" height="702">https://s/1-1248.png</image>
        <image type="thumbnail" width="752" height="423">https://s/1-752.png</image>
        <image type="thumbnail" width="224" height="126">https://s/1-224.png</image>
      </screenshot>
      <screenshot>
        <image type="source" xml:lang="de">https://s/2-de.png</image>
        <image type="source">https://s/2.png</image>
        <image type="thumbnail" width="224" height="126">https://s/2-224.png</image>
      </screenshot>
      <screenshot><video>https://s/3.webm</video></screenshot>
      <screenshot><image type="source" xml:lang="de">https://s/4-de.png</image></screenshot>
    </screenshots>""")
    # The smallest thumbnail at least as wide as asked, else the untranslated original; a
    # video and a screenshot with only a translated image give nothing
    assert mod.screenshot_urls(cpt) == ["https://s/1-752.png", "https://s/2.png"]
    assert mod.screenshot_urls(cpt, width=1000) == ["https://s/1-1248.png", "https://s/2.png"]


def test_release_version(mod):
    new, none = components(mod, "<id>a</id><name>A</name><releases><release version='2.0' date='2026-02-01'/>"
                                "<release version='1.0' date='2025-01-01'/></releases>",
                           "<id>b</id><name>B</name>")
    assert mod.release_version(new) == "2.0" and mod.release_version(none) == ""


def test_desktop_info_of_a_missing_launcher(mod):
    assert mod.desktop_info(None) is None and mod.desktop_info("") is None
    assert mod.desktop_info("org.plinius.test.does-not-exist.desktop") is None


class FakeInfo:
    def __init__(self, show=True, name="Shown Name", factors=""):
        self.show, self.name, self.factors = show, name, factors

    def should_show(self):
        return self.show

    def get_display_name(self):
        return self.name


def test_app_entry(mod, monkeypatch):
    infos = {"org.a.desktop": FakeInfo(show=False)}
    monkeypatch.setattr(mod, "desktop_info", infos.get)
    a, b = components(mod, """<id>org.a</id><name>Alpha</name><summary>Chat &amp; more</summary>
        <keywords><keyword>Messenger</keyword></keywords>
        <categories><category>Network</category><category>Chat</category></categories>
        <icon type="stock">alpha</icon><launchable type="desktop-id">org.a.desktop</launchable>
        <requires><display_length compare="ge">360</display_length></requires>""",
                      "<id>org.b</id>")
    app = mod.App(a, mod.PACMAN, "alpha", "1.0-1", None, True)
    assert (app.id, app.name, app.summary, app.pkg, app.version) == ("org.a", "Alpha", "Chat & more", "alpha", "1.0-1")
    assert app.rating == "phone" and app.note == mod.NOTES["phone"]
    assert app.search == "alpha chat & more alpha messenger"
    assert app.categories == {"Network", "Chat"} and app.icon_name == "alpha"
    assert app.desktop_id == "org.a.desktop" and app.flatpak_id is None
    # Installed with NoDisplay: hidden (a helper pulled in as a dependency)
    assert app.hidden is True
    assert app.removal is None and app.recommended is False
    # apps.conf's rating and note win over the app's own data
    assert mod.App(a, mod.PACMAN, "alpha", "", ("desktop", "Too wide"), False).note == "Too wide"
    not_installed = mod.App(a, mod.PACMAN, "alpha", "", ("scaled", ""), False)
    assert not_installed.hidden is False and not_installed.note == mod.NOTES["scaled"]
    # Without a name, a summary and a launcher
    flat = mod.App(b, mod.FLATPAK, "app/org.b.Real/aarch64/stable", "", None, True)
    assert (flat.name, flat.summary, flat.desktop_id, flat.icon_name) == ("org.b", "", None, None)
    assert flat.flatpak_id == "org.b.Real" and flat.rating == "unknown" and flat.hidden is False


def test_lines_of_a_program(mod):
    assert mod.lines("printf", "a\\nb\\n") == ["a", "b"]
    assert mod.lines("/nonexistent/program") == []


class FakePool:
    """AppStream.Pool over parsed components instead of the system catalog."""

    def __init__(self, cpts):
        self.cpts, self.flags, self.loaded = cpts, None, False

    def set_flags(self, flags):
        self.flags = flags

    def load(self, _cancellable):
        self.loaded = True
        return True

    def get_components_by_kind(self, kind):
        assert kind == AS.ComponentKind.DESKTOP_APP and self.loaded
        box = AS.ComponentBox.new(AS.ComponentBoxFlags.NO_CHECKS)
        for c in self.cpts:
            box.add(c)
        return box


AS = None


@pytest.fixture
def catalog(mod, sw, monkeypatch):
    """load_catalog() over the given components and package state."""
    global AS
    AS = mod.AppStream
    monkeypatch.setattr(mod, "desktop_info", lambda desktop_id: None)

    def run(cpts, sl=(), qq=(), qi="", flathub=False, flatpaks=(), ratings=None, recommended=()):
        pools = []

        def pool():
            pools.append(FakePool(cpts))
            return pools[-1]
        monkeypatch.setattr(mod, "AppStream", types.SimpleNamespace(
            Pool=pool, PoolFlags=AS.PoolFlags, ComponentKind=AS.ComponentKind, BundleKind=AS.BundleKind,
            LaunchableKind=AS.LaunchableKind, RelationItemKind=AS.RelationItemKind,
            DisplaySideKind=AS.DisplaySideKind, RelationCompare=AS.RelationCompare, IconKind=AS.IconKind))
        answers = {("pacman", "-Sl"): list(sl), ("pacman", "-Qq"): list(qq),
                   ("env", "LC_ALL=C", "pacman", "-Qi"): qi.splitlines(),
                   ("flatpak", "remotes", "--system", "--columns=name"): ["flathub"] if flathub else [],
                   ("flatpak", "list", "--system", "--app", "--columns=application"): list(flatpaks)}
        monkeypatch.setattr(mod, "lines", lambda *cmd: answers[cmd])
        if ratings is not None:
            (sw / "ratings").write_text(ratings)
        (sw / "recommended").write_text("".join(p + "\n" for p in recommended))
        apps = mod.load_catalog()
        return apps, pools[0]
    return run


def test_load_catalog_only_what_the_repositories_have(mod, catalog):
    cpts = components(mod,
                      "<id>org.zed</id><name>zed</name><pkgname>zed</pkgname>",
                      "<id>org.x86</id><name>Only on x86</name><pkgname>x86-only</pkgname>",
                      "<id>org.alpha</id><name>Alpha</name><pkgname>alpha</pkgname>",
                      "<id>org.gcc</id><name>Settings</name><pkgname>gnome-control-center</pkgname>",
                      "<id>org.fox</id><name>Firefox</name><pkgname>firefox</pkgname>")
    apps, pool = catalog(
        cpts,
        sl=["extra zed 1-1", "extra alpha 2-1 [installed]", "core alpha 9-9", "plinius gnome-control-center-plinius 51-1",
            "extra gnome-control-center 50-1", "extra firefox 140-1", "broken"],
        qq=["alpha", "gnome-control-center-plinius", "firefox", "firefox-i18n-de"],
        qi="Name : gnome-control-center-plinius\nProvides : gnome-control-center=51\nRequired By : plinius-apps\n"
           "Name : firefox\nRequired By : firefox-i18n-de\nName : firefox-i18n-de\nRequired By : None\n"
           "Name : alpha\nRequired By : None\n",
        ratings="org.zed desktop\n", recommended=["zed", "firefox"])
    assert pool.flags == AS.PoolFlags.LOAD_OS_CATALOG
    # Sorted by name regardless of case; the x86-only package is left out
    assert [a.name for a in apps] == ["Alpha", "Firefox", "Settings", "zed"]
    alpha, fox, gcc, zed = apps
    assert (alpha.pkg, alpha.version, alpha.installed, alpha.removal) == ("alpha", "2-1", True, ["alpha"])
    # The custom build that provides the Arch package is the one installed, and it is needed
    assert (gcc.pkg, gcc.version, gcc.installed, gcc.removal) == ("gnome-control-center-plinius", "51-1", True, None)
    assert fox.removal == ["firefox", "firefox-i18n-de"] and fox.recommended
    assert (zed.installed, zed.removal, zed.recommended, zed.rating) == (False, None, True, "desktop")


def test_load_catalog_with_flathub(mod, catalog):
    cpts = components(
        mod,
        # Only on Flathub, installed / not installed
        "<id>org.flat</id><name>Flat</name><bundle type='flatpak'>app/org.flat/aarch64/stable</bundle>"
        "<releases><release version='3.1' date='2026-01-01'/></releases>",
        "<id>org.new</id><name>New</name><bundle type='flatpak'>app/org.new.App/aarch64/stable</bundle>",
        # In both sources: the installed one wins, else the one from the repositories
        "<id>org.both</id><name>Both</name><pkgname>both</pkgname>",
        "<id>org.both</id><name>Both</name><bundle type='flatpak'>app/org.both/aarch64/stable</bundle>",
        "<id>org.inst</id><name>Inst</name><bundle type='flatpak'>app/org.inst/aarch64/stable</bundle>",
        "<id>org.inst</id><name>Inst</name><pkgname>inst</pkgname>")
    apps, pool = catalog(cpts, sl=["extra both 1-1", "extra inst 1-1"], flathub=True,
                         flatpaks=["org.flat", "org.inst"])
    assert pool.flags == AS.PoolFlags.LOAD_OS_CATALOG | AS.PoolFlags.LOAD_FLATPAK
    got = {a.id: (a.source, a.pkg, a.version, a.installed, a.removal) for a in apps}
    assert got == {
        "org.flat": ("flatpak", "app/org.flat/aarch64/stable", "3.1", True, ["org.flat"]),
        "org.new": ("flatpak", "app/org.new.App/aarch64/stable", "", False, None),
        "org.both": ("pacman", "both", "1-1", False, None),
        "org.inst": ("flatpak", "app/org.inst/aarch64/stable", "", True, ["org.inst"]),
    }


def test_load_catalog_installed_flatpaks_are_not_asked_without_flathub(mod, catalog):
    cpts = components(mod, "<id>org.flat</id><name>Flat</name><bundle type='flatpak'>app/org.flat/aarch64/stable</bundle>")
    apps, _pool = catalog(cpts, flathub=False, flatpaks=["org.flat"])
    assert [(a.id, a.installed) for a in apps] == [("org.flat", False)]


# --- Image loading ------------------------------------------------------------------------------

def png_bytes():
    """A 1x1 PNG."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\xff")) + chunk(b"IEND", b""))


def spin(mod, until, timeout=5):
    """Run the default main context until until() is true."""
    ctx = mod.GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not until():
        assert time.monotonic() < end, "timed out"
        ctx.iteration(False)
        time.sleep(0.001)


def test_loader_shares_one_load_and_caches_when_asked(mod):
    import threading
    gate = threading.Event()
    loads = []

    def load(key):
        loads.append(key)
        gate.wait(5)
        if key == "broken":
            raise OSError("unreadable")
        return "texture:" + key

    for keep in (True, False):
        loads.clear()
        gate.clear()
        loader = mod.Loader(keep=keep)
        got = []
        loader.get("a", load, lambda t: got.append(("first", t)))
        loader.get("a", load, lambda t: got.append(("second", t)))
        loader.get("broken", load, lambda t: got.append(("broken", t)))
        gate.set()
        spin(mod, lambda: len(got) == 3)
        assert got == [("first", "texture:a"), ("second", "texture:a"), ("broken", None)]
        assert loads == ["a", "broken"]  # the second request for "a" waited for the first load
        loader.get("a", load, lambda t: got.append(("again", t)))
        if keep:
            assert got[-1] == ("again", "texture:a") and loads == ["a", "broken"]
        else:
            spin(mod, lambda: len(got) == 4)
            assert got[-1] == ("again", "texture:a") and loads == ["a", "broken", "a"]


def test_load_file_gives_a_texture(mod, tmp_path):
    f = tmp_path / "icon.png"
    f.write_bytes(png_bytes())
    texture = mod.load_file(str(f))
    assert (texture.get_width(), texture.get_height()) == (1, 1)
    with pytest.raises(mod.GLib.Error):
        mod.load_file(str(tmp_path / "missing.png"))


class FakeSoup:
    """Soup without the network: messages get the status the test gives the URL."""

    def __init__(self, mod, answers):
        self.mod, self.answers, self.sessions, self.sent = mod, answers, [], []
        self.Status = mod.Soup.Status
        outer = self

        class Message:
            @staticmethod
            def new(method, url):
                if url not in answers:
                    return None
                return types.SimpleNamespace(url=url, method=method, get_status=lambda: answers[url][0])
        self.Message = Message

        class Session:
            def __init__(self, **props):
                outer.sessions.append(props)

            def send_and_read(self, message, _cancellable):
                outer.sent.append((message.method, message.url))
                return mod.GLib.Bytes.new(answers[message.url][1])
        self.Session = Session


def test_load_url_only_https_and_only_a_good_answer(mod, monkeypatch):
    import threading
    soup = FakeSoup(mod, {"https://shots/ok.png": (mod.Soup.Status.OK, png_bytes()),
                          "https://shots/gone.png": (mod.Soup.Status.NOT_FOUND, b"not found")})
    monkeypatch.setattr(mod, "Soup", soup)
    monkeypatch.setattr(mod, "_session", threading.local())
    # Plain http could be swapped or watched on the way: not even asked
    assert mod.load_url("http://shots/ok.png") is None and soup.sessions == []
    assert mod.load_url("https://shots/gone.png") is None
    texture = mod.load_url("https://shots/ok.png")
    assert texture.get_width() == 1
    assert mod.load_url("https://invalid url") is None
    # One session for the thread, with a timeout
    assert soup.sessions == [{"timeout": 30, "user_agent": "plinius-software"}]
    assert soup.sent == [("GET", "https://shots/gone.png"), ("GET", "https://shots/ok.png")]


# --- Notifications, Settings and logind over D-Bus (a fake bus) -----------------------------------

class FakeBus:
    def __init__(self, mod, replies=None):
        self.mod, self.calls, self.subscribed, self.watched = mod, [], [], []
        self.replies = replies or {}

    def call_sync(self, name, path, iface, method, params, reply_type, flags, timeout):
        self.calls.append((name, path, iface, method, params.unpack() if params else None, timeout))
        answer = self.replies.get(method)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, callback):
        self.subscribed.append((sender, iface, member, path, callback))

    def emit(self, signal, *params):
        for _s, _i, member, _p, callback in self.subscribed:
            if member == signal:
                callback(self, ":1.5", "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
                         signal, params)


@pytest.fixture
def bus(mod, monkeypatch):
    """Session and system bus as FakeBus objects; the main loop runs the test's script."""
    real_gio, real_glib = mod.Gio, mod.GLib
    buses = {real_gio.BusType.SESSION: FakeBus(mod, {"Notify": (17,)}),
             real_gio.BusType.SYSTEM: FakeBus(mod)}
    state = types.SimpleNamespace(buses=buses, script=lambda bus, loop: None, quit=0, ran=0)

    class Loop:
        def run(self):
            state.ran += 1
            state.script(buses[real_gio.BusType.SESSION], self)

        def quit(self):
            state.quit += 1

    def watch(connection, name, flags, appeared, vanished):
        connection.watched.append((name, vanished))

    monkeypatch.setattr(mod, "Gio", Proxy(real_gio, bus_get_sync=lambda kind: buses[kind],
                                          bus_watch_name_on_connection=watch))
    monkeypatch.setattr(mod, "GLib", Proxy(real_glib, MainLoop=Loop))
    return state


class Proxy:
    def __init__(self, real, **overrides):
        self._real = real
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_update_notification_tapped_opens_settings_on_updates(mod, bus):
    def tap(session, loop):
        session.emit("ActionInvoked", 16, "default")  # someone else's notification
        assert bus.quit == 0
        session.emit("ActionInvoked", 17, "default")
    bus.script = tap
    mod.send_update_notification("System update available", "2 packages can be updated.")
    session = bus.buses[mod.Gio.BusType.SESSION]
    notify, panel = session.calls
    assert notify[:4] == ("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                          "org.freedesktop.Notifications", "Notify")
    assert notify[4] == ("Software", 0, "software-update-available-symbolic", "System update available",
                         "2 packages can be updated.", ["default", "Go to Updates"],
                         {"desktop-entry": "org.gnome.Settings"}, -1)
    assert panel[:4] == ("org.gnome.Settings", "/org/gnome/Settings", "org.freedesktop.Application", "ActivateAction")
    assert panel[4] == ("launch-panel", [("updates", [])], {})
    assert bus.ran == 1 and bus.quit == 1
    assert sorted(s[2] for s in session.subscribed) == ["ActionInvoked", "NotificationClosed"]


def test_update_notification_dismissed_or_phosh_gone(mod, bus):
    bus.script = lambda session, loop: session.emit("NotificationClosed", 17, 2)
    mod.send_update_notification("s", "b")
    session = bus.buses[mod.Gio.BusType.SESSION]
    assert [c[3] for c in session.calls] == ["Notify"] and bus.quit == 1
    # The notification server leaves (Phosh restarted): nobody will tap it
    name, vanished = session.watched[-1]
    assert name == "org.freedesktop.Notifications"
    vanished(session, name)
    assert bus.quit == 2


def test_settings_that_cannot_be_reached(mod, bus, capsys):
    session = bus.buses[mod.Gio.BusType.SESSION]
    session.replies["ActivateAction"] = mod.GLib.Error("Settings is not there")
    bus.script = lambda s, loop: s.emit("ActionInvoked", 17, "default")
    mod.send_update_notification("s", "b")
    assert bus.quit == 1  # the process does not stay around
    assert "could not open Settings on Updates: Settings is not there" in capsys.readouterr().err


def test_reboot_asks_logind_interactively(mod, bus):
    mod.reboot()
    system = bus.buses[mod.Gio.BusType.SYSTEM]
    assert system.calls == [("org.freedesktop.login1", "/org/freedesktop/login1",
                             "org.freedesktop.login1.Manager", "Reboot", (True,), -1)]


def test_shown_settings_with_one_key(mod, sw):
    (sw / "settings").write_text("[Anzeige]\nshow-desktop=true\n")
    assert mod.load_shown() == {"phone", "scaled", "desktop"}
    (sw / "settings").write_text("[Anzeige]\nshow-unknown=maybe\n")
    assert mod.load_shown() == {"phone", "scaled"}


def test_an_unknown_locale_does_not_stop_the_app(monkeypatch):
    import locale

    def refuse(category, value=None):
        if value == "":
            raise locale.Error("unsupported locale setting")
        return "C"
    monkeypatch.setattr(locale, "setlocale", refuse)
    m = load_script(SCRIPT, "script_plinius_software_bad_locale")
    assert m.LABELS["phone"] == "For phones" and m._("Software") == "Software"


def test_launch_of_an_app_without_a_launcher_does_nothing(mod, monkeypatch):
    started = []
    monkeypatch.setattr(mod, "desktop_info", lambda desktop_id: None)
    monkeypatch.setattr(mod, "Gio", Proxy(mod.Gio, Subprocess=types.SimpleNamespace(
        new=lambda argv, flags: started.append(argv))))
    cpt, = components(mod, "<id>org.a</id><name>A</name>")
    mod.launch(mod.App(cpt, mod.PACMAN, "a", "", None, True))
    assert started == []
    mod.launch(mod.App(cpt, mod.FLATPAK, "app/org.a.Real/aarch64/stable", "", None, True))
    assert started == [["flatpak", "run", "org.a.Real"]]


def test_progress_hook_lines_without_a_counter(mod):
    p = mod.Progress([mod.INSTALL, mod.HOOKS])
    p.feed(":: Running post-transaction hooks...")
    p.feed("(1/3) Arming ConditionNeedsUpdate...")
    p.feed("  -> a hook's own output")
    assert p.count() == (1, 3)


# --- The command line (the timer and Settings) ---------------------------------------------------

@pytest.fixture
def program(tmp_path, fake_bin):
    """Runs plinius-software as the timer or Settings do: checkupdates finds one update,
    plinius-freigabe and Flathub are not there, no display, no bus."""
    if os.path.exists("/usr/lib/plinius-software/plinius-repo-stamp"):
        pytest.skip("the host has plinius-repo-stamp installed; it would be run for real")
    fake_bin.stub("checkupdates", '[ -n "$CHECKUPDATES_DB" ] && echo "glibc 2.43-1 -> 2.44-1"')
    fake_bin.stub("pacman")
    fake_bin.stub("flatpak")
    state = tmp_path / "state" / "plinius-software"
    env = fake_bin.env(isolated=True, HOME=tmp_path / "home", XDG_CONFIG_HOME=tmp_path / "config",
                       XDG_STATE_HOME=tmp_path / "state", XDG_RUNTIME_DIR=tmp_path / "run",
                       XDG_CACHE_HOME=tmp_path / "cache",
                       DBUS_SESSION_BUS_ADDRESS=f"unix:path={tmp_path}/no-bus",
                       DBUS_SYSTEM_BUS_ADDRESS=f"unix:path={tmp_path}/no-bus", GSETTINGS_BACKEND="memory")
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "GDK_BACKEND"):
        env.pop(name, None)

    def run(*args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True,
                              timeout=60)
    return run, state


def test_updates_json_on_the_command_line(program):
    run, state = program
    r = run("--updates-json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["code"] == 0 and out["packages"] == [["glibc", "2.43-1", "2.44-1"]]
    assert out["flatpaks"] == [] and out["held"] is None
    assert out["last_check"].startswith("today at ")
    # Without the stamp helper the server's state is unproven
    assert out["repo_warning"] == "The update server gives no proof of how current its updates are."
    assert (state / "last-check").exists()


def test_check_updates_on_the_command_line_tells_nothing_twice(program):
    run, state = program
    state.mkdir(parents=True)
    # The timer already told about this update and this warning; no notification (and no bus
    # that could show one) is needed
    (state / "notified").write_text("glibc 2.44-1")
    (state / "repo-warning").write_text("The update server gives no proof of how current its updates are.")
    (state / "held-notified").write_text("new plinius-freigabe")
    r = run("--check-updates")
    assert r.returncode == 0, r.stderr
    assert (state / "notified").read_text() == "glibc 2.44-1"
    assert not (state / "held-notified").exists()  # the hold is over
