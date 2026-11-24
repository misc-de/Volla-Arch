# tools/check-conflicts.py (file conflicts of our packages with ALARM) and tools/ueventd2udev.py
# (udev rules from a vendor ueventd.rc), on synthetic inputs.

import subprocess
import sys

import pytest

from conftest import TOOLS, load_script
from release_sim import write_db, write_tar

CONFLICTS = TOOLS / "check-conflicts.py"
UEVENTD = TOOLS / "ueventd2udev.py"


@pytest.fixture
def dbdir(tmp_path):
    d = tmp_path / "db"
    write_db(d / "core.files.tar.gz", {
        "systemd-259.1-1": {"FILES": ["usr/", "usr/lib/", "usr/lib/systemd/systemd"]},
        "gnome-control-center-51.1-1": {"FILES": ["usr/bin/gnome-control-center"]}}, member="files")
    write_db(d / "extra.files.tar.gz", {
        "phosh-0.57.0-1": {"FILES": ["usr/bin/phosh", "usr/share/phosh/"]}}, member="files")
    # no alarm repo (x86_64 preview): skipped
    return d


def pkg(tmp_path, name, files, conflicts=()):
    info = f"pkgname = {name}\npkgver = 1-1\n" + "".join(f"conflict = {c}\n" for c in conflicts)
    members = {".PKGINFO": info, ".MTREE": "x", "usr/": None}
    members.update({f: "content" for f in files})
    return write_tar(tmp_path / f"{name}-1-1-aarch64.pkg.tar.zst", members)


def run(*args):
    return subprocess.run([sys.executable, str(CONFLICTS), *map(str, args)], capture_output=True,
                          text=True, timeout=30)


def test_alarm_files_maps_paths_to_packages(dbdir):
    cc = load_script(CONFLICTS)
    owner = cc.alarm_files(str(dbdir))
    assert owner == {"usr/lib/systemd/systemd": "core/systemd-259.1-1",
                     "usr/bin/gnome-control-center": "core/gnome-control-center-51.1-1",
                     "usr/bin/phosh": "extra/phosh-0.57.0-1"}


def test_conflict_reported(dbdir, tmp_path):
    p = pkg(tmp_path, "phosh-extra", ["usr/bin/phosh", "usr/bin/own"])
    r = run(dbdir, p)
    assert r.returncode == 1
    assert r.stdout.splitlines() == [
        "phosh-extra-1-1-aarch64.pkg.tar.zst: usr/bin/phosh  <->  extra/phosh-0.57.0-1",
        "1 conflict(s)"]


def test_declared_conflicts_are_replaced_on_purpose(dbdir, tmp_path):
    p = pkg(tmp_path, "gnome-control-center-plinius", ["usr/bin/gnome-control-center"],
            conflicts=["gnome-control-center>=1", "phosh<1:9"])
    q = pkg(tmp_path, "phosh-plinius", ["usr/bin/phosh"], conflicts=["phosh"])
    r = run(dbdir, p, q)
    assert r.returncode == 0, r.stdout
    assert r.stdout.strip() == "0 conflict(s)"


def test_directories_and_metadata_never_conflict(dbdir, tmp_path):
    p = pkg(tmp_path, "x", ["usr/share/phosh/x"])
    assert run(dbdir, p).returncode == 0


def test_usage(dbdir):
    r = run(dbdir)
    assert r.returncode == 1 and "Usage" in r.stderr


# --- ueventd2udev ---------------------------------------------------------------------------------

UEVENTD_RC = """\
# vendor ueventd.rc
/dev/wmtWifi              0660   wifi       wifi
/dev/stpbt                0660   bluetooth  radio
/dev/block/by-name/nvram  0660   root       system
/dev/dri/card0            0666   system     graphics
/dev/hidraw*              0660   system     system
/dev/ttyMT*               0660   radio      radio
/dev/ccci_fs              0660   radio      radio
/dev/ccci_fs              0660   radio      radio
/dev/video/m4u            0666   media      camera
/sys/devices/platform/x  enable  0660  system system
"""


def test_rules_for_vendor_nodes(tmp_path):
    rc = tmp_path / "ueventd.rc"
    rc.write_text(UEVENTD_RC)
    r = subprocess.run([sys.executable, str(UEVENTD), str(rc)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    rules = [l for l in r.stdout.splitlines() if not l.startswith("#")]
    assert rules == [
        'ACTION=="add|change", KERNEL=="wmtWifi", OWNER="aid_wifi", GROUP="aid_wifi", MODE="0660"',
        'ACTION=="add|change", KERNEL=="stpbt", OWNER="aid_bluetooth", GROUP="aid_radio", MODE="0660"',
        'ACTION=="add|change", KERNEL=="ttyMT*", OWNER="aid_radio", GROUP="aid_radio", MODE="0660"',
        'ACTION=="add|change", KERNEL=="ccci_fs", OWNER="aid_radio", GROUP="aid_radio", MODE="0660"',
        'ACTION=="add|change", SUBSYSTEM=="video", KERNEL=="m4u", OWNER="aid_media", '
        'GROUP="aid_camera", MODE="0666"']


def test_root_stays_root(tmp_path):
    u = load_script(UEVENTD)
    assert list(u.rules(["/dev/x 0600 root system\n"])) == [
        'ACTION=="add|change", KERNEL=="x", OWNER="root", GROUP="aid_system", MODE="0600"']


def test_unknown_android_id_stops(tmp_path):
    rc = tmp_path / "ueventd.rc"
    rc.write_text("/dev/x 0660 nosuchuser system\n")
    r = subprocess.run([sys.executable, str(UEVENTD), str(rc)], capture_output=True, text=True)
    assert r.returncode == 1 and "unknown Android ID" in r.stderr


def test_sysusers_has_every_aid_but_root():
    r = subprocess.run([sys.executable, str(UEVENTD), "--sysusers"], capture_output=True, text=True)
    users = [l for l in r.stdout.splitlines() if l.startswith("u ")]
    assert 'u aid_wifi 1010 "Android wifi" - -' in users
    assert not any(" root " in l or "aid_root" in l for l in users)
    u = load_script(UEVENTD)
    assert len(users) == len(u.AIDS) - 1


@pytest.mark.parametrize("args", [[], ["a.rc", "b.rc"], ["--sysusers", "x"]])
def test_ueventd2udev_usage(args):
    r = subprocess.run([sys.executable, str(UEVENTD), *args], capture_output=True, text=True)
    assert r.returncode == 1 and r.stdout == ""
    assert "ueventd.rc" in r.stderr  # the usage text
