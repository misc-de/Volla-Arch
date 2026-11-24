# plinius-pkg upgrade (pkgbuilds/plinius-software): the system update as two pacman runs with the
# stamp check of [plinius] in between (plinius-repo-stamp, verify_repo): what each exit code of
# the check does to the update (install, fetch again, warn and install, stop with exit 12), that
# the sync databases are put back whenever nothing is installed (restore_sync), and that a hold
# by plinius-freigabe still leaves the system alone.
#
# The whole script runs, as a copy in tmp_path whose system paths all point into tmp_path, as
# the unit's inner invocation (PLINIUS_PKG_UNIT=1). pacman, pacman-conf, plinius-repo-stamp,
# plinius-restart, id ("root"), systemd-inhibit, pgrep and sleep are stubs on an isolated PATH.

import re
import subprocess

import pytest

from conftest import PKG, bash_functions

SCRIPT = PKG / "plinius-software" / "plinius-pkg"
# An absolute path into the running system, outside comments (as in tests/release/release_sim.py)
SYSTEM_PATH = re.compile(r"(?<![\w.$}/@-])/(dev|proc|sys|usr|var|run|etc|boot)(/|\b)")

# -Sy writes a new plinius.db (numbered per run), adds new.db and drops gone.db; -Su "updates"
# glibc. SY_RCS / STAMP_RCS: the exit codes of the n-th run, 0 when not given.
PACMAN = r'''
mode=$1
for a; do case $a in -Sy|-Su|-Slq|-Qqo) mode=$a ;; esac; done
nth() { local n=$(( $(cat "$STATE/$1" 2>/dev/null || echo 0) + 1 )); echo $n > "$STATE/$1"; echo $n; }
case $mode in
-Q) [[ -e $STATE/upgraded ]] && echo "glibc 2" || echo "glibc 1"; echo "zlib 1" ;;
-Sy)
	n=$(nth sy)
	echo "pacman -Sy" >> "$ORDER"
	rc=$(echo "${SY_RCS:-0} " | cut -d' ' -f$n)
	if [[ ${rc:-0} == 0 ]]; then
		printf 'database of sync %s' $n > "$SYNC/plinius.db"
		echo added > "$SYNC/new.db"
		rm -f "$SYNC/gone.db"
	else
		echo "error: failed to synchronize all databases"
	fi
	exit ${rc:-0} ;;
-Su)
	echo "pacman $*" >> "$ORDER"
	[[ -n ${SU_OUT:-} ]] && printf '%s\n' "$SU_OUT"
	[[ ${SU_RC:-0} == 0 ]] && touch "$STATE/upgraded"
	exit ${SU_RC:-0} ;;
-Slq) exit 1 ;;
esac
exit 0
'''

STAMP = r'''
n=$(( $(cat "$STATE/stamp" 2>/dev/null || echo 0) + 1 )); echo $n > "$STATE/stamp"
[[ $1 == check && $2 == --db && $4 == --text && $# == 4 ]] || { echo "bad call: $*" >&2; exit 99; }
echo "stamp: $(cat "$3")" >> "$ORDER"
rc=$(echo "${STAMP_RCS:-0} " | cut -d' ' -f$n)
echo "plinius-repo-stamp: verdict $n"
exit ${rc:-0}
'''

RESTART = r'''
case $1 in
snapshot) echo "units" ;;
check) echo '{"level": "none"}' ;;
esac
'''


@pytest.fixture
def upgrade(fake_bin, tmp_path):
    root = tmp_path / "root"
    paths = {
        "RUN=/run/plinius-pkg": f"RUN={root}/run",
        "RESTART=/usr/lib/plinius-software/plinius-restart": f"RESTART={fake_bin.path}/plinius-restart",
        "STAMP=/usr/lib/plinius-software/plinius-repo-stamp": f"STAMP={fake_bin.path}/plinius-repo-stamp",
        "/etc/locale.conf": f"{root}/locale.conf",
        "/run/plinius-pkg.lock": f"{root}/lock",
        "/sys/kernel/security/lsm": f"{root}/lsm",
        "/var/lib/plinius-pkg/overwritten": f"{root}/overwritten",
        "/etc/.pacman.conf.XXXXXX": f"{root}/.pacman.conf.XXXXXX",
        "/etc/pacman.conf": f"{root}/pacman.conf",
        "/var/lib/pacman/db.lck": f"{root}/db.lck",
        "/sys/class/power_supply": f"{root}/power_supply",
    }
    text = SCRIPT.read_text()
    for old, new in paths.items():
        assert old in text, f"{old} no longer in plinius-pkg: update the test"
        text = text.replace(old, new)
    for n, line in enumerate(text.splitlines(), 1):
        if not line.lstrip().startswith("#"):
            assert not SYSTEM_PATH.search(line.replace("/dev/null", "")), f"line {n}: {line.strip()}"
    script = tmp_path / "plinius-pkg"
    script.write_text(text)
    (root / "run").mkdir(parents=True)
    (root / "power_supply").mkdir()
    (root / "lsm").write_text("capability,landlock,yama\n")
    sync = root / "dbpath" / "sync"
    sync.mkdir(parents=True)
    before = {"plinius.db": "database before", "core.db": "core before", "gone.db": "only before"}
    for name, content in before.items():
        (sync / name).write_text(content)
    state = tmp_path / "state"
    state.mkdir()
    order = tmp_path / "order"
    fake_bin.stub("id", 'echo 0')
    fake_bin.stub("pacman", PACMAN)
    fake_bin.stub("pacman-conf", f'[ "$1" = DBPath ] && echo "{root}/dbpath/"')
    fake_bin.stub("plinius-repo-stamp", STAMP)
    fake_bin.stub("plinius-restart", RESTART)
    fake_bin.stub("systemd-inhibit", 'while [[ $1 == --* ]]; do shift; done; exec "$@"')
    fake_bin.stub("pgrep", "exit 1")
    fake_bin.stub("sleep")

    class Upgrade:
        run_dir = root / "run"

        @staticmethod
        def __call__(**env):
            r = subprocess.run(["bash", str(script), "upgrade"], capture_output=True, text=True, timeout=60,
                               env=fake_bin.env(isolated=True, PLINIUS_PKG_UNIT=1, STATE=state, ORDER=order,
                                                SYNC=sync, **env))
            Upgrade.order = order.read_text().splitlines() if order.exists() else []
            return r

        @staticmethod
        def sync_dir():
            return {p.name: p.read_text() for p in sync.iterdir()}

        @staticmethod
        def sleeps():
            return fake_bin.calls("sleep")

    Upgrade.before = before
    return Upgrade()


def put_back(upgrade):
    """Nothing was installed and the sync databases are as before the upgrade."""
    assert upgrade.sync_dir() == upgrade.before
    assert not (upgrade.run_dir / "sync.before").exists()
    assert not any("-Su" in step for step in upgrade.order)


def test_stamp_checked_between_sync_and_upgrade(upgrade):
    r = upgrade()
    assert r.returncode == 0, r.stdout + r.stderr
    # The check sees the database just fetched, before anything is installed
    assert upgrade.order == ["pacman -Sy", "stamp: database of sync 1",
                             "pacman --noconfirm --noprogressbar -Su"]
    assert "plinius-repo-stamp: verdict 1" in r.stdout
    assert upgrade.sync_dir()["plinius.db"] == "database of sync 1"
    assert not (upgrade.run_dir / "sync.before").exists()
    assert (upgrade.run_dir / "restart.json").read_text() == '{"level": "none"}\n'
    assert (upgrade.run_dir / "packages.changed").read_text() == "glibc\n"
    assert upgrade.sleeps() == []


def test_genuine_but_old_stamp_still_updates(upgrade):
    r = upgrade(STAMP_RCS="2")
    assert r.returncode == 0
    assert upgrade.order[-1].endswith("-Su")


@pytest.mark.parametrize("rc", [1, 99, 127])
def test_wrong_stamp_installs_nothing_and_puts_the_databases_back(upgrade, rc):
    # 1: foreign or replayed stamp, other channel, unreadable database; others: the check failed
    r = upgrade(STAMP_RCS=str(rc))
    assert r.returncode == 12
    assert "does not match the signed stamp of its server; nothing was installed" in r.stderr
    put_back(upgrade)
    assert not (upgrade.run_dir / "restart.json").exists()


def test_mismatch_fetches_both_once_more(upgrade):
    # An upload switching database and stamp over: after a pause, both are fetched again
    r = upgrade(STAMP_RCS="3 0")
    assert r.returncode == 0
    assert upgrade.order == ["pacman -Sy", "stamp: database of sync 1", "pacman -Sy",
                             "stamp: database of sync 2", "pacman --noconfirm --noprogressbar -Su"]
    assert upgrade.sleeps() == [["15"]]


@pytest.mark.parametrize("rcs", ["3 3", "3 1"])
def test_mismatch_twice_stops(upgrade, rcs):
    r = upgrade(STAMP_RCS=rcs)
    assert r.returncode == 12
    assert upgrade.order[:4] == ["pacman -Sy", "stamp: database of sync 1", "pacman -Sy",
                                 "stamp: database of sync 2"]
    put_back(upgrade)


def test_mismatch_and_the_second_sync_fails(upgrade):
    # Fetching once more failed: nothing could be checked, which is a failed sync (classify,
    # Software's "Update failed" with the details), not a package list against its stamp (12)
    r = upgrade(STAMP_RCS="3", SY_RCS="0 1")
    assert r.returncode == 1
    assert upgrade.order == ["pacman -Sy", "stamp: database of sync 1", "pacman -Sy"]
    assert ("plinius-pkg: fetching the package lists once more (after a stamp that did not match "
            "yet) failed; nothing was installed") in r.stderr
    assert "does not match the signed stamp" not in r.stderr
    assert "error: failed to synchronize all databases" in r.stdout
    put_back(upgrade)
    assert not (upgrade.run_dir / "restart.json").exists()


def test_mismatch_and_the_second_sync_finds_the_database_gone(upgrade, fake_bin):
    # The server answers 404 on the second fetch: classify's code for an outdated state (4)
    fake_bin.stub("pacman", PACMAN.replace('echo "error: failed to synchronize all databases"',
                                           'echo "error: The requested URL returned error: 404"'))
    r = upgrade(STAMP_RCS="3", SY_RCS="0 1")
    assert r.returncode == 4
    assert "fetching the package lists once more" in r.stderr
    put_back(upgrade)


@pytest.mark.parametrize("rcs", ["4", "3 4"])
def test_no_stamp_to_be_had_warns_and_updates(upgrade, rcs):
    # A server problem never holds back security updates (for now)
    r = upgrade(STAMP_RCS=rcs)
    assert r.returncode == 0
    assert "plinius-pkg: warning: the stamp of [plinius] could not be checked, updating anyway" in r.stdout
    assert upgrade.order[-1].endswith("-Su")


def test_failed_sync_puts_the_databases_back_without_a_stamp_check(upgrade):
    r = upgrade(SY_RCS="1")
    assert r.returncode == 1
    assert upgrade.order == ["pacman -Sy"]
    assert upgrade.sync_dir() == upgrade.before


def test_hold_by_approval_puts_the_databases_back(upgrade):
    r = upgrade(SU_RC=1, SU_OUT=":: plinius-freigabe-5-1 and glibc-2.45-1 are in conflict")
    # Nothing changed on the system: "nothing to do" (no Flatpak here)
    assert r.returncode == 6
    assert "system packages wait for approval" in r.stderr
    assert upgrade.sync_dir() == upgrade.before
    assert not (upgrade.run_dir / "sync.before").exists()


def test_failed_upgrade_keeps_the_new_databases(upgrade):
    # A pacman error after a good stamp: the databases are the verified ones, and -Su failed
    # for its own reason (classify)
    r = upgrade(SU_RC=1, SU_OUT="error: The requested URL returned error: 404")
    assert r.returncode == 4
    assert upgrade.sync_dir()["plinius.db"] == "database of sync 1"


# --- restore_sync on its own ---------------------------------------------------------------------

def test_restore_sync_also_removes_what_the_sync_added(fake_bin, tmp_path):
    sync, before = tmp_path / "sync", tmp_path / "run" / "sync.before"
    sync.mkdir()
    before.mkdir(parents=True)
    (before / "plinius.db").write_text("old")
    (before / "plinius.db.sig").symlink_to("elsewhere")
    (before / "core.db").write_text("old core")
    (sync / "plinius.db").write_text("new")
    (sync / "extra.db").write_text("added")
    # A link the sync left is replaced, not written through
    (tmp_path / "outside").write_text("must stay")
    (sync / "core.db").symlink_to(tmp_path / "outside")
    code = bash_functions(SCRIPT, "restore_sync") + f'RUN={tmp_path}/run\nsync={sync}\nrestore_sync\n'
    r = subprocess.run(["bash", "-c", "set -euo pipefail\n" + code], env=fake_bin.env(isolated=True),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in sync.iterdir()) == ["core.db", "plinius.db", "plinius.db.sig"]
    assert (sync / "plinius.db").read_text() == "old" and (sync / "core.db").read_text() == "old core"
    assert not (sync / "core.db").is_symlink() and (tmp_path / "outside").read_text() == "must stay"
    assert (sync / "plinius.db.sig").readlink().name == "elsewhere"
