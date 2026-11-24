# plinius-rollback: which transaction counts as the last update and which cache files take it
# back. pacman, pacman-conf, bsdtar and id are stubs; the log comes from PLINIUS_ROLLBACK_LOG.

import subprocess

import pytest

from conftest import PKG

SRC = PKG / "plinius-rollback" / "plinius-rollback"


def tx(time, *lines, completed=True):
    out = [f"[{time}] [PACMAN] Running 'pacman -Syu'", f"[{time}] [ALPM] transaction started"]
    out += [f"[{time}] [ALPM] {l}" for l in lines]
    out.append(f"[{time}] [ALPM-SCRIPTLET] ==> Some hook output")
    if completed:
        out.append(f"[{time}] [ALPM] transaction completed")
    return "\n".join(out) + "\n"


class Rollback:
    def __init__(self, tmp_path, fake_bin):
        self.log = tmp_path / "pacman.log"
        self.cache = tmp_path / "cache"
        self.cache.mkdir()
        self.fake_bin = fake_bin
        fake_bin.stub("pacman-conf", f'[[ $1 == CacheDir ]] && echo "{self.cache}/"')
        # A cache "package" is a text file holding its .PKGINFO
        fake_bin.stub("bsdtar", '[[ $1 == -xOf && $3 == .PKGINFO ]] && cat "$2"')
        fake_bin.stub("pacman")
        fake_bin.stub("id", '[[ $1 == -u ]] && echo "${FAKE_UID:-0}"')

    def cached(self, name, version, filename=None, ext="zst"):
        f = self.cache / (filename or f"{name}-{version}-aarch64.pkg.tar.{ext}")
        f.write_text(f"pkgname = {name}\npkgver = {version}\n")
        return f

    def run(self, *args, uid=0):
        env = self.fake_bin.env(isolated=True, PLINIUS_ROLLBACK_LOG=self.log, FAKE_UID=uid)
        return subprocess.run(["bash", str(SRC), *args], env=env, capture_output=True, text=True,
                              timeout=30)

    def pacman(self):
        return self.fake_bin.calls("pacman")


@pytest.fixture
def rb(tmp_path, fake_bin):
    return Rollback(tmp_path, fake_bin)


def test_run_installs_the_previous_versions_in_one_transaction(rb):
    rb.log.write_text(
        tx("2026-11-10T08:00:00+0100", "upgraded foo (0.9-1 -> 1.0-1)")
        + tx("2026-11-12T06:32:11+0100", "upgraded foo (1.0-1 -> 1.1-1)",
             "upgraded lib-bar (2:3.0-2 -> 2:3.1-1)", "installed newdep (1.0-1)",
             "removed oldpkg (4.2-1)"))
    foo = rb.cached("foo", "1.0-1")
    bar = rb.cached("lib-bar", "2:3.0-2")
    old = rb.cached("oldpkg", "4.2-1", ext="xz")
    r = rb.run("run", "--noconfirm")
    assert r.returncode == 0, r.stderr
    assert rb.pacman() == [["-U", "--noconfirm", str(foo), str(bar), str(old)]]
    assert "Last update: 2026-11-12T06:32:11+0100, 3 previous versions in the cache, 0 missing" in r.stdout
    assert "Installed new by it, stays: newdep" in r.stdout


def test_list_changes_nothing(rb):
    rb.log.write_text(tx("2026-11-12T06:32:11+0100", "upgraded foo (1.0-1 -> 1.1-1)"))
    rb.cached("foo", "1.0-1")
    r = rb.run("list", uid=1000)
    assert r.returncode == 0, r.stderr
    assert "foo 1.0-1  (upgraded, in the cache)" in r.stdout
    assert rb.pacman() == []


def test_run_needs_root(rb):
    rb.log.write_text(tx("2026-11-12T06:32:11+0100", "upgraded foo (1.0-1 -> 1.1-1)"))
    r = rb.run("run", uid=1000)
    assert r.returncode == 1 and "must run as root" in r.stderr
    assert rb.pacman() == []


def test_a_missing_previous_version_changes_nothing(rb):
    rb.log.write_text(tx("2026-11-12T06:32:11+0100", "upgraded foo (1.0-1 -> 1.1-1)",
                         "upgraded baz (5-1 -> 6-1)"))
    rb.cached("foo", "1.0-1")
    r = rb.run("run")
    assert r.returncode == 3
    assert "baz 5-1  (upgraded, MISSING from the cache)" in r.stdout
    assert rb.pacman() == []


def test_the_last_transaction_with_an_upgrade_counts(rb):
    # A later transaction that only installed or downgraded (a rollback) is not an update
    rb.log.write_text(
        tx("2026-11-11T10:00:00+0100", "upgraded foo (1.0-1 -> 1.1-1)")
        + tx("2026-11-11T11:00:00+0100", "installed extra (1-1)")
        + tx("2026-11-11T12:00:00+0100", "downgraded foo (1.1-1 -> 1.0-1)"))
    foo = rb.cached("foo", "1.0-1")
    r = rb.run("run")
    assert r.returncode == 0, r.stderr
    assert rb.pacman() == [["-U", str(foo)]]
    assert "2026-11-11T10:00:00+0100" in r.stdout


def test_an_interrupted_transaction_does_not_count(rb):
    rb.log.write_text(
        tx("2026-11-11T10:00:00+0100", "upgraded foo (1.0-1 -> 1.1-1)")
        + tx("2026-11-12T10:00:00+0100", "upgraded foo (1.1-1 -> 1.2-1)", completed=False))
    foo = rb.cached("foo", "1.0-1")
    r = rb.run("run")
    assert r.returncode == 0, r.stderr
    assert rb.pacman() == [["-U", str(foo)]]


def test_cache_file_of_a_longer_version_is_not_taken(rb):
    # "foo-1.0-1-*" also matches foo 1.0-1.1 (pkgrel 1.1) and the signature
    rb.log.write_text(tx("2026-11-12T06:32:11+0100", "upgraded foo (1.0-1 -> 1.1-1)"))
    rb.cached("foo", "1.0-1.1", filename="foo-1.0-1-1-aarch64.pkg.tar.zst")
    (rb.cache / "foo-1.0-1-aarch64.pkg.tar.zst.sig").write_text("sig")
    r = rb.run("run")
    assert r.returncode == 3
    assert rb.pacman() == []
    good = rb.cached("foo", "1.0-1")
    r = rb.run("run")
    assert r.returncode == 0
    assert rb.pacman() == [["-U", str(good)]]


@pytest.mark.parametrize("log", ["", tx("2026-11-12T06:32:11+0100", "installed foo (1.0-1)")])
def test_nothing_to_roll_back(rb, log):
    rb.log.write_text(log)
    r = rb.run("run")
    assert r.returncode == 4
    assert rb.pacman() == []


def test_usage(rb):
    rb.log.write_text("")
    r = rb.run("undo")
    assert r.returncode == 1 and "usage" in r.stderr


# --- summary and run --unit: for GNOME Settings › System › Self-Test ---

def test_summary_for_settings(rb):
    rb.log.write_text(tx("2026-11-12T06:32:11+0100", "upgraded foo (1.0-1 -> 1.1-1)",
                         "upgraded bar (2-1 -> 3-1)", "installed newdep (1.0-1)"))
    rb.cached("foo", "1.0-1")
    rb.cached("bar", "2-1")
    r = rb.run("summary", uid=1000)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "when=2026-11-12T06:32:11+0100\npackages=2\nmissing=0\n"
    assert rb.pacman() == []


def test_summary_with_a_missing_previous_version(rb):
    rb.log.write_text(tx("2026-11-12T06:32:11+0100", "upgraded foo (1.0-1 -> 1.1-1)",
                         "upgraded baz (5-1 -> 6-1)"))
    rb.cached("foo", "1.0-1")
    r = rb.run("summary", uid=1000)
    assert r.returncode == 3
    assert r.stdout == "when=2026-11-12T06:32:11+0100\npackages=1\nmissing=1\n"


@pytest.mark.parametrize("log", ["", tx("2026-11-12T06:32:11+0100", "installed foo (1.0-1)")])
def test_summary_without_an_update(rb, log):
    rb.log.write_text(log)
    r = rb.run("summary", uid=1000)
    assert r.returncode == 4
    assert r.stdout == ""


def test_run_as_a_unit(rb):
    # The rollback runs in a system unit of its own: Settings ending cannot stop pacman
    rb.fake_bin.stub("systemd-run", "exit 3")
    r = rb.run("run", "--noconfirm", "--unit")
    assert r.returncode == 3  # the unit's status comes back
    (call,) = rb.fake_bin.calls("systemd-run")
    assert call[:6] == ["--quiet", "--wait", "--collect", "--service-type=exec", "--unit=plinius-rollback",
                        "--description=Take back the last system update"]
    assert call[6:] == [str(SRC.resolve()), "run", "--noconfirm"]
    assert "--pipe" not in call
    assert rb.pacman() == []


def test_run_as_a_unit_needs_root(rb):
    rb.fake_bin.stub("systemd-run")
    r = rb.run("run", "--unit", uid=1000)
    assert r.returncode == 1 and "must run as root" in r.stderr
    assert rb.fake_bin.calls("systemd-run") == []


def test_unknown_option(rb):
    rb.log.write_text("")
    r = rb.run("run", "--yes")
    assert r.returncode == 1 and "unknown option: --yes" in r.stderr
