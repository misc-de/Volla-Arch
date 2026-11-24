# plinius-epo (orbit predictions for mnld: mnl.prop as the gps user, the download, clear) and
# plinius-epo-set (the switch in GNOME Settings, run through pkexec). Only copies with their paths
# in tmp_path are run; setpriv runs the copy again as the test user, curl serves files from a
# directory in tmp_path, lxc-info/lxc-attach stand in for the Android container.

import hashlib
import os
import stat

import pytest

from conftest import PKG
from system_helpers import rewritten_copy, run

EPO = PKG / "plinius-epo" / "plinius-epo"
EPO_SET = PKG / "plinius-epo" / "plinius-epo-set"
SHIPPED_CONF = (PKG / "plinius-epo" / "plinius-epo.conf").read_text()

EPO_HOST = "uepodownload.mediatek.com"
NAV_HOST = "ugepodownload.mediatek.com"
NOW = 1_800_000_000
SIX_HOURS = 6 * 3600

# (remote, local, md5 file, host) as plinius-epo's FILES
FILES = [("EPO_GR_3_1.DAT", "EPO.DAT", "EPO_GR_3_1.MD5", EPO_HOST),
         ("QG_R.DAT", "QEPO.DAT", "QG_R.MD5", EPO_HOST),
         ("QGA.DAT", "QEPO_GA.DAT", "QGA.MD5", EPO_HOST),
         ("QBD2.DAT", "QEPO_BD.DAT", "QBD2.MD5", EPO_HOST),
         ("MTKNAV.DAT", "MTKNAV.DAT", "MTKNAV.MD5", NAV_HOST)]
LOCAL_NAMES = [f[1] for f in FILES]


def mnl_prop(on):
    return (f"EPO_enabled={on}\nEPO_priority=64\nSUPL_enabled=0\nSUPLSI_enabled=0\n"
            "EPO_DOWNLOAD_ENABLE=0\nQEPO_DOWNLOAD_ENABLE=0\n")


class Epo:
    def __init__(self, tmp_path, fake_bin):
        self.tmp = tmp_path
        self.bin = fake_bin
        self.conf = tmp_path / "etc" / "plinius-epo.conf"
        self.gps = tmp_path / "android" / "data" / "vendor" / "gps"
        self.state = tmp_path / "var" / "lib" / "plinius-epo"
        self.server = tmp_path / "server"
        for d in (self.conf.parent, self.gps, self.state, self.server):
            d.mkdir(parents=True)
        self.conf.write_text(SHIPPED_CONF)
        self.script = tmp_path / "plinius-epo"
        rewritten_copy(EPO, self.script, {
            "/etc/plinius-epo.conf": self.conf,
            "/android/data/vendor/gps": self.gps,
            "/var/lib/plinius-epo": self.state,
            # as_gps runs this copy again, not the installed script
            "/usr/lib/plinius-epo/plinius-epo": self.script,
        })
        b = fake_bin
        # setpriv: drop its options, run the command (as the test user, not as gps)
        b.stub("setpriv", 'while [ "$1" != -- ]; do shift; done; shift; exec "$@"')
        b.stub("lxc-info", 'echo "State:          ${LXC_STATE:-RUNNING}"')
        b.stub("lxc-attach", 'exit "${LXC_ATTACH_RC:-0}"')
        b.stub("md5sum", 'exec /usr/bin/md5sum "$@"')
        b.stub("date", 'echo "${FAKE_NOW}"')
        # curl: each URL is served from server/<host>/<path> into the -o before it; CURL_FAIL
        # names files that fail (ALL: no network)
        b.stub("curl", f'''
out=; rc=0
while [ $# -gt 0 ]; do
	case $1 in
		--proto|--connect-timeout|--max-time) shift 2 ;;
		-o) out=$2; shift 2 ;;
		-*) shift ;;
		*)
			url=$1; shift
			case " ${{CURL_FAIL:-}} " in *" ALL "*) exit 6 ;; *" ${{url##*/}} "*) rc=22; continue ;; esac
			f="{self.server}/${{url#https://}}"
			if [ -f "$f" ]; then cp "$f" "$out"; else rc=22; fi
			;;
	esac
done
exit $rc''')

    def serve(self, remote, data, host=EPO_HOST, md5=None, md5_name=None):
        d = self.server / host
        d.mkdir(parents=True, exist_ok=True)
        (d / remote).write_bytes(data)
        md5 = md5 if md5 is not None else hashlib.md5(data).hexdigest()
        md5_name = md5_name or remote.replace(".DAT", ".MD5")
        (d / md5_name).write_text(f"{md5}    \n")

    def serve_all(self):
        for i, (remote, _local, _md5, host) in enumerate(FILES):
            # EPO records are 72 bytes; MTKNAV is not checked for that
            data = bytes([i + 1]) * (72 * (i + 2)) if remote != "MTKNAV.DAT" else b"nav" * 33
            self.serve(remote, data, host)

    def run(self, *args, **env):
        e = {"FAKE_NOW": NOW}
        e.update(env)
        return run(["bash", self.script, *args], self.bin.env(isolated=True, **e))

    def tmp_dirs(self):
        return [p.name for p in self.gps.iterdir() if p.name.startswith(".plinius-epo.")]


@pytest.fixture
def epo(tmp_path, fake_bin):
    return Epo(tmp_path, fake_bin)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


# ----- apply / write-prop -----

def test_epo_apply_writes_mnl_prop_as_gps_and_restarts_mnld(epo):
    r = epo.run("apply")
    assert r.returncode == 0, r.stderr
    prop = epo.gps / "mnl.prop"
    assert prop.read_text() == mnl_prop(1)
    assert mode(prop) == 0o664
    assert "<5>mnl.prop: EPO_enabled=1" in r.stdout
    # The file work runs as gps: no groups, no capabilities, no way back, a clean environment
    assert epo.bin.calls("setpriv") == [[
        "--reuid=1021", "--regid=1021", "--clear-groups", "--no-new-privs", "--reset-env",
        "--", str(epo.script), "write-prop", "1"]]
    assert epo.bin.calls("lxc-attach") == [
        ["-n", "android", "--", "/system/bin/setprop", "ctl.restart", "mnld"]]
    # No temporary file is left next to it
    assert [p.name for p in epo.gps.iterdir()] == ["mnl.prop"]


def test_epo_apply_unchanged_file_does_not_restart_mnld(epo):
    (epo.gps / "mnl.prop").write_text(mnl_prop(1))
    r = epo.run("apply")
    assert r.returncode == 0, r.stderr
    assert epo.bin.calls("lxc-attach") == []
    assert "mnl.prop" not in r.stdout


def test_epo_apply_off_follows_the_setting(epo):
    (epo.gps / "mnl.prop").write_text(mnl_prop(1))
    epo.conf.write_text(SHIPPED_CONF.replace("enabled=yes", "enabled=no"))
    r = epo.run("apply")
    assert r.returncode == 0, r.stderr
    assert (epo.gps / "mnl.prop").read_text() == mnl_prop(0)
    assert epo.bin.calls("setpriv")[0][-2:] == ["write-prop", "0"]
    assert len(epo.bin.calls("lxc-attach")) == 1


@pytest.mark.parametrize("conf, on", [
    (None, 1),                    # no config file: on, as shipped
    ("enabled=\n", 1),            # empty value: the default
    ("enabled=no\nenabled=yes\n", 1),  # the last line counts
    ("enabled=yes\nenabled=no\n", 0),
    ("enabled=off\n", 0),         # anything but yes is off
    ("#enabled=no\n", 1),
])
def test_epo_apply_reads_the_switch(epo, conf, on):
    if conf is None:
        epo.conf.unlink()
    else:
        epo.conf.write_text(conf)
    assert epo.run("apply").returncode == 0
    assert (epo.gps / "mnl.prop").read_text() == mnl_prop(on)


def test_epo_apply_with_mnld_stopped_only_writes(epo):
    r = epo.run("apply", LXC_STATE="STOPPED")
    assert r.returncode == 0, r.stderr
    assert (epo.gps / "mnl.prop").read_text() == mnl_prop(1)
    assert epo.bin.calls("lxc-attach") == []


def test_epo_apply_failed_restart_is_a_warning(epo):
    r = epo.run("apply", LXC_ATTACH_RC=1)
    assert r.returncode == 0, r.stderr
    assert "<4>mnld not restarted" in r.stdout


def test_epo_apply_replaces_a_link_without_touching_its_target(epo, tmp_path):
    # gps could swap mnl.prop for a link to someone else's file: the link is replaced
    victim = tmp_path / "victim"
    victim.write_text(mnl_prop(1))
    (epo.gps / "mnl.prop").symlink_to(victim)
    r = epo.run("apply")
    assert r.returncode == 0, r.stderr
    prop = epo.gps / "mnl.prop"
    assert not prop.is_symlink() and prop.read_text() == mnl_prop(1)
    assert victim.read_text() == mnl_prop(1)
    assert len(epo.bin.calls("lxc-attach")) == 1


def test_epo_apply_replaces_a_fifo_without_reading_it(epo):
    os.mkfifo(epo.gps / "mnl.prop")
    r = epo.run("apply")  # would hang (timeout) if it read the FIFO
    assert r.returncode == 0, r.stderr
    prop = epo.gps / "mnl.prop"
    assert stat.S_ISREG(prop.lstat().st_mode) and prop.read_text() == mnl_prop(1)


def test_epo_apply_without_the_gps_directory(epo):
    epo.gps.rmdir()
    r = epo.run("apply")
    assert r.returncode == 0, r.stderr
    assert not epo.gps.exists()
    assert epo.bin.calls("lxc-attach") == []


def test_epo_apply_passes_on_a_failed_write(epo):
    epo.bin.stub("setpriv", "exit 1")
    r = epo.run("apply")
    assert r.returncode == 1
    assert "<4>mnl.prop not written (status 1)" in r.stdout
    assert epo.bin.calls("lxc-attach") == []


@pytest.mark.parametrize("arg", [None, "2", "11", "1; touch x", ""])
def test_epo_write_prop_takes_only_0_or_1(epo, arg):
    args = ["write-prop"] + ([] if arg is None else [arg])
    r = epo.run(*args)
    assert r.returncode == 2
    assert not (epo.gps / "mnl.prop").exists()


def test_epo_write_prop_status_tells_changed(epo):
    assert epo.run("write-prop", "0").returncode == 3
    assert epo.run("write-prop", "0").returncode == 0
    assert epo.run("write-prop", "1").returncode == 3


# ----- fetch -----

def test_epo_fetch_downloads_all_files(epo):
    epo.serve_all()
    r = epo.run("fetch")
    assert r.returncode == 0, r.stderr
    for remote, local, _md5, host in FILES:
        f = epo.gps / local
        assert f.read_bytes() == (epo.server / host / remote).read_bytes()
        assert mode(f) == 0o664
    # Only the checksums mnld keeps itself stay next to the data
    names = sorted(p.name for p in epo.gps.iterdir())
    assert names == sorted(LOCAL_NAMES + ["EPO_GR_3_1.MD5", "MTKNAV.MD5"])
    assert mode(epo.gps / "EPO_GR_3_1.MD5") == 0o664
    assert (epo.state / "last").read_text() == f"{NOW}\n"
    assert mode(epo.state / "last") == 0o644
    assert "<5>EPO updated (5 of 5 files)" in r.stdout
    # Only HTTPS, each URL with its checksum in one call
    calls = epo.bin.calls("curl")
    assert len(calls) == 5
    for c in calls:
        assert c[c.index("--proto") + 1] == "=https" and "--tlsv1.2" in c and "-f" in "".join(c)
    first = calls[0][-6:]
    assert first[0] == "-o" and first[1].endswith("/EPO_GR_3_1.DAT")
    assert first[2] == f"https://{EPO_HOST}/EPO_GR_3_1.DAT"
    assert first[3] == "-o" and first[4].endswith("/EPO_GR_3_1.MD5")
    assert first[5] == f"https://{EPO_HOST}/EPO_GR_3_1.MD5"
    # Downloaded into a directory in mnld's, so the files move in at once
    assert os.path.dirname(first[1]).startswith(str(epo.gps / ".plinius-epo."))
    urls = [a for c in calls for a in c if a.startswith("https://")]
    assert f"https://{NAV_HOST}/MTKNAV.DAT" in urls and f"https://{EPO_HOST}/QBD2.MD5" in urls


def test_epo_fetch_uses_the_configured_mirrors(epo):
    epo.conf.write_text("enabled=yes\nepo_url=https://mirror.test/epo\nnav_url=https://nav.test\n")
    for i, (remote, _local, _md5, host) in enumerate(FILES):
        epo.serve(remote, b"x" * 72 * (i + 1), "nav.test" if host == NAV_HOST else "mirror.test/epo")
    r = epo.run("fetch")
    assert r.returncode == 0, r.stderr
    assert (epo.gps / "MTKNAV.DAT").read_bytes() == b"x" * 72 * 5
    assert "<5>EPO updated (5 of 5 files)" in r.stdout


@pytest.mark.parametrize("last, age_ok", [
    (NOW - 60, True),                 # fetched a minute ago
    (NOW - SIX_HOURS + 1, True),
    (NOW - SIX_HOURS, False),         # six hours: again
    (NOW + 3600, False),              # stamp from the future (clock was wrong): again
    ("junk", False),
])
def test_epo_fetch_only_every_six_hours(epo, last, age_ok):
    epo.serve_all()
    (epo.gps / "EPO.DAT").write_bytes(b"old" * 24)
    (epo.state / "last").write_text(f"{last}\n")
    r = epo.run("fetch")
    assert r.returncode == 0, r.stderr
    assert (epo.bin.calls("curl") == []) == age_ok
    assert ((epo.gps / "EPO.DAT").read_bytes() == b"old" * 24) == age_ok


def test_epo_fetch_again_when_the_three_day_file_is_gone_or_empty(epo):
    epo.serve_all()
    (epo.state / "last").write_text(f"{NOW - 60}\n")
    assert epo.run("fetch").returncode == 0
    assert len(epo.bin.calls("curl")) == 5
    (epo.gps / "EPO.DAT").write_bytes(b"")
    (epo.state / "last").write_text(f"{NOW - 60}\n")
    assert epo.run("fetch").returncode == 0
    assert len(epo.bin.calls("curl")) == 10
    assert (epo.gps / "EPO.DAT").stat().st_size > 0


def test_epo_fetch_without_network_tries_again_later(epo):
    (epo.gps / "EPO.DAT").write_bytes(b"old" * 24)
    r = epo.run("fetch", CURL_FAIL="ALL")
    # 75: SuccessExitStatus= of the service, the unit is not failed
    assert r.returncode == 75
    assert "<4>EPO not downloaded (EPO_GR_3_1.DAT: download), next try within an hour" in r.stdout
    assert (epo.gps / "EPO.DAT").read_bytes() == b"old" * 24
    assert not (epo.state / "last").exists()
    assert epo.tmp_dirs() == []
    # It stops at the first file
    assert len(epo.bin.calls("curl")) == 1


@pytest.mark.parametrize("what, why", [
    ("checksum", "checksum"), ("empty", "checksum"), ("size", "size"), ("no-md5", "download")])
def test_epo_fetch_refuses_a_bad_three_day_file(epo, what, why):
    epo.serve_all()
    d = epo.server / EPO_HOST
    if what == "checksum":
        (d / "EPO_GR_3_1.MD5").write_text("0" * 32 + "\n")
    elif what == "empty":
        epo.serve("EPO_GR_3_1.DAT", b"")
    elif what == "size":
        epo.serve("EPO_GR_3_1.DAT", b"x" * 100)
    else:
        (d / "EPO_GR_3_1.MD5").unlink()
    r = epo.run("fetch")
    assert r.returncode == 75
    assert f"(EPO_GR_3_1.DAT: {why})" in r.stdout
    assert [p.name for p in epo.gps.iterdir()] == []
    assert not (epo.state / "last").exists()


def test_epo_fetch_checksum_in_upper_case_is_accepted(epo):
    epo.serve_all()
    data = (epo.server / EPO_HOST / "EPO_GR_3_1.DAT").read_bytes()
    (epo.server / EPO_HOST / "EPO_GR_3_1.MD5").write_text(
        hashlib.md5(data).hexdigest().upper() + "  EPO_GR_3_1.DAT\n")
    r = epo.run("fetch")
    assert r.returncode == 0, r.stderr
    assert (epo.gps / "EPO.DAT").read_bytes() == data


def test_epo_fetch_mtknav_needs_no_record_size(epo):
    epo.serve_all()
    epo.serve("MTKNAV.DAT", b"n" * 101, NAV_HOST)
    epo.serve("QGA.DAT", b"q" * 101)
    r = epo.run("fetch")
    assert r.returncode == 0, r.stderr
    assert (epo.gps / "MTKNAV.DAT").read_bytes() == b"n" * 101
    assert not (epo.gps / "QEPO_GA.DAT").exists()
    assert "<4>not downloaded: QGA.DAT (size)" in r.stdout


def test_epo_fetch_other_files_failing_is_a_warning(epo):
    epo.serve_all()
    epo.serve("QG_R.DAT", b"r" * 72, md5="f" * 32)
    r = epo.run("fetch", CURL_FAIL="QBD2.DAT MTKNAV.MD5")
    assert r.returncode == 0, r.stderr
    assert "<4>not downloaded: QG_R.DAT (checksum) QBD2.DAT (download) MTKNAV.DAT (download)" \
        in r.stdout
    assert "<5>EPO updated (2 of 5 files)" in r.stdout
    assert sorted(p.name for p in epo.gps.iterdir()) == ["EPO.DAT", "EPO_GR_3_1.MD5", "QEPO_GA.DAT"]
    # The stamp is written: the next try is in six hours, with the three-day file in place
    assert (epo.state / "last").read_text() == f"{NOW}\n"
    assert epo.tmp_dirs() == []


def test_epo_fetch_replaces_the_old_files(epo):
    epo.serve_all()
    (epo.gps / "EPO.DAT").write_bytes(b"old")
    (epo.gps / "EPO_GR_3_1.MD5").write_text("old\n")
    assert epo.run("fetch").returncode == 0
    data = (epo.server / EPO_HOST / "EPO_GR_3_1.DAT").read_bytes()
    assert (epo.gps / "EPO.DAT").read_bytes() == data
    assert (epo.gps / "EPO_GR_3_1.MD5").read_text().split()[0] == hashlib.md5(data).hexdigest()


def test_epo_fetch_without_the_gps_directory(epo):
    epo.gps.rmdir()
    r = epo.run("fetch")
    assert r.returncode == 0, r.stderr
    assert f"<4>{epo.gps} missing, nothing fetched" in r.stdout
    assert epo.bin.calls("curl") == []


def test_epo_fetch_switched_off_removes_the_files(epo):
    epo.serve_all()
    assert epo.run("fetch").returncode == 0
    (epo.gps / "mnl.prop").write_text(mnl_prop(0))
    (epo.gps / "other.dat").write_text("mnld's own\n")
    epo.conf.write_text(SHIPPED_CONF.replace("enabled=yes", "enabled=no"))
    r = epo.run("fetch")
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in epo.gps.iterdir()) == ["mnl.prop", "other.dat"]
    assert not (epo.state / "last").exists()
    assert "<5>EPO files removed" in r.stdout
    assert len(epo.bin.calls("curl")) == 5
    # Nothing left to remove: no message
    r = epo.run("fetch")
    assert r.returncode == 0 and "removed" not in r.stdout


# ----- clear, usage -----

def test_epo_clear_switches_off_and_removes_as_gps(epo):
    epo.serve_all()
    assert epo.run("fetch").returncode == 0
    (epo.gps / "mnl.prop").write_text(mnl_prop(1))
    r = epo.run("clear")
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in epo.gps.iterdir()) == ["mnl.prop"]
    assert (epo.gps / "mnl.prop").read_text() == mnl_prop(0)
    assert not (epo.state / "last").exists()
    assert [c[-2:] if c[-1] in "01" else c[-1:] for c in epo.bin.calls("setpriv")] == [
        ["write-prop", "0"], ["remove-files"]]
    assert all(c[:5] == ["--reuid=1021", "--regid=1021", "--clear-groups", "--no-new-privs",
                         "--reset-env"] for c in epo.bin.calls("setpriv"))
    assert len(epo.bin.calls("lxc-attach")) == 1


def test_epo_clear_when_already_clear(epo):
    (epo.gps / "mnl.prop").write_text(mnl_prop(0))
    r = epo.run("clear")
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
    assert epo.bin.calls("lxc-attach") == []


@pytest.mark.parametrize("args", [[], ["help"], ["APPLY"]])
def test_epo_usage(epo, args):
    r = epo.run(*args)
    assert r.returncode == 2
    assert "usage: plinius-epo apply|fetch|clear" in r.stderr


# ----- plinius-epo-set -----

class EpoSet:
    def __init__(self, tmp_path, fake_bin):
        self.bin = fake_bin
        self.conf = tmp_path / "etc" / "plinius-epo.conf"
        self.conf.parent.mkdir(parents=True)
        self.conf.write_text(SHIPPED_CONF)
        self.script = rewritten_copy(EPO_SET, tmp_path / "plinius-epo-set",
                                     {"/etc/plinius-epo.conf": self.conf})
        fake_bin.stub("systemctl")

    def run(self, *args):
        return run(["bash", self.script, *args], self.bin.env(isolated=True))


@pytest.fixture
def epo_set(tmp_path, fake_bin):
    return EpoSet(tmp_path, fake_bin)


def test_epo_set_off_and_on(epo_set):
    r = epo_set.run("off")
    assert r.returncode == 0, r.stderr
    assert epo_set.conf.read_text() == SHIPPED_CONF.replace("enabled=yes", "enabled=no")
    assert epo_set.run("on").returncode == 0
    # Everything else in the file stays as it was
    assert epo_set.conf.read_text() == SHIPPED_CONF
    assert epo_set.bin.calls("systemctl") == [["start", "--no-block", "plinius-epo.service"]] * 2


def test_epo_set_adds_a_missing_line(epo_set):
    epo_set.conf.write_text("epo_url=https://mirror.test\n")
    assert epo_set.run("off").returncode == 0
    assert epo_set.conf.read_text() == "epo_url=https://mirror.test\nenabled=no\n"
    epo_set.conf.unlink()
    assert epo_set.run("on").returncode == 0
    assert epo_set.conf.read_text() == "enabled=yes\n"


def test_epo_set_leaves_commented_lines(epo_set):
    epo_set.conf.write_text("# enabled=yes\nenabled=yes\n")
    assert epo_set.run("off").returncode == 0
    assert epo_set.conf.read_text() == "# enabled=yes\nenabled=no\n"


def test_epo_set_appends_on_a_line_of_its_own(epo_set):
    epo_set.conf.write_text("nav_url=https://nav.test")
    assert epo_set.run("off").returncode == 0
    assert epo_set.conf.read_text() == "nav_url=https://nav.test\nenabled=no\n"
    # A value line without a newline at the end is set as well
    epo_set.conf.write_text("a=1\nenabled=no")
    assert epo_set.run("on").returncode == 0
    assert epo_set.conf.read_text() == "a=1\nenabled=yes\n"


def test_epo_set_keeps_one_enabled_line(epo_set):
    epo_set.conf.write_text("enabled=yes\nx= y \n\nenabled=yes\n  indented\n")
    assert epo_set.run("off").returncode == 0
    assert epo_set.conf.read_text() == "enabled=no\nx= y \n\n  indented\n"


def test_epo_set_writes_a_new_file_not_through_a_link(epo_set, tmp_path):
    # Renamed over the path: a link there is replaced, its target stays as it was
    target = tmp_path / "elsewhere"
    target.write_text("enabled=yes\nother=1\n")
    epo_set.conf.unlink()
    epo_set.conf.symlink_to(target)
    assert epo_set.run("off").returncode == 0
    assert not epo_set.conf.is_symlink()
    assert epo_set.conf.read_text() == "enabled=no\nother=1\n"
    assert target.read_text() == "enabled=yes\nother=1\n"
    assert stat.S_IMODE(epo_set.conf.stat().st_mode) == 0o644
    # Nothing left next to it
    assert sorted(p.name for p in epo_set.conf.parent.iterdir()) == ["plinius-epo.conf"]


def test_epo_set_leaves_no_temporary_file_when_it_fails(epo_set):
    # The configuration cannot be read: the switch fails and the old file stays
    if os.geteuid() == 0:
        pytest.skip("root reads the file anyway")
    epo_set.conf.chmod(0)
    try:
        r = epo_set.run("off")
    finally:
        epo_set.conf.chmod(0o644)
    assert r.returncode != 0
    assert epo_set.conf.read_text() == SHIPPED_CONF
    assert sorted(p.name for p in epo_set.conf.parent.iterdir()) == ["plinius-epo.conf"]
    assert epo_set.bin.calls("systemctl") == []


@pytest.mark.parametrize("args", [[], ["yes"], ["on; reboot"], ["ON"]])
def test_epo_set_usage(epo_set, args):
    r = epo_set.run(*args)
    assert r.returncode == 2
    assert "usage: plinius-epo-set on|off" in r.stderr
    assert epo_set.conf.read_text() == SHIPPED_CONF
    assert epo_set.bin.calls("systemctl") == []
