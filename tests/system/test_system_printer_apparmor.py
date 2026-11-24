# plinius-pdf-printer (keeps a PDF queue that is never the default next to a real printer) and
# plinius-apparmor-load (loads only the phone's own profiles), with their install scripts and
# the "expected" list GNOME Settings compares against. lpstat/lpadmin/apparmor_parser are stubs.

import re
import subprocess

import pytest

from conftest import PKG
from system_helpers import call_scriptlet, rewritten_copy, run, scriptlet

PDF = PKG / "plinius-pdf-printer"
AA = PKG / "plinius-apparmor"


# --- plinius-pdf-printer ---

def printer(fake_bin, *, running=True, pdf=True, default=None, dests=("PDF",)):
    fake_bin.stub("lpstat", f'''
[ "$LC_ALL" = C ] || exit 9
case $1 in
-r) exit {0 if running else 1} ;;
-p) [ "$2" = PDF ] && exit {0 if pdf else 1}; exit 1 ;;
-d) {f'echo "system default destination: {default}"' if default else 'echo "no system default destination"'} ;;
-e) printf "%s\\n" {" ".join(dests)} ;;
esac''')
    fake_bin.stub("lpadmin")
    r = run(["sh", PDF / "plinius-pdf-printer"], fake_bin.env(isolated=True, LC_ALL="de_DE.UTF-8"))
    assert r.returncode == 0, r.stderr
    return fake_bin.calls("lpadmin")


def test_pdf_nothing_without_scheduler(fake_bin):
    assert printer(fake_bin, running=False, pdf=False) == []


def test_pdf_queue_created_and_made_default_when_alone(fake_bin):
    calls = printer(fake_bin, pdf=False, dests=())
    assert calls[0][:6] == ["-p", "PDF", "-E", "-v", "cups-pdf:/", "-m"]
    assert "printer-is-shared=false" in calls[0]
    assert calls[1:] == [["-d", "PDF"]]


def test_pdf_real_printer_replaces_pdf_as_default(fake_bin):
    assert printer(fake_bin, default="PDF", dests=("PDF", "Laser")) == [["-d", "Laser"]]


def test_pdf_real_printer_becomes_default_when_none_is_set(fake_bin):
    assert printer(fake_bin, dests=("PDF", "Office")) == [["-d", "Office"]]


def test_pdf_users_choice_of_a_real_printer_stays(fake_bin):
    assert printer(fake_bin, default="Office", dests=("PDF", "Laser", "Office")) == []


def test_pdf_stays_default_when_alone(fake_bin):
    # Nothing to change: no lpadmin, so its own write does not start the path unit again
    assert printer(fake_bin, default="PDF", dests=("PDF",)) == []


def test_pdf_similar_name_is_a_real_printer(fake_bin):
    assert printer(fake_bin, default="PDF", dests=("PDF", "PDF-Office")) == [["-d", "PDF-Office"]]


def test_pdf_install_enables_and_starts_only_on_running_system(tmp_path, fake_bin):
    fake_bin.stub("systemctl")
    run_dir = tmp_path / "run-systemd"
    script = scriptlet("plinius-pdf-printer", tmp_path, {"/run/systemd/system": run_dir})
    env = fake_bin.env(isolated=True)
    assert call_scriptlet(script, "post_install", env).returncode == 0
    assert fake_bin.calls("systemctl") == [
        ["enable", "plinius-pdf-printer.service", "plinius-pdf-printer.path"]]
    run_dir.mkdir()
    assert call_scriptlet(script, "post_install", env).returncode == 0
    assert fake_bin.calls("systemctl")[1:] == [
        ["enable", "plinius-pdf-printer.service", "plinius-pdf-printer.path"],
        ["start", "plinius-pdf-printer.path"], ["start", "plinius-pdf-printer.service"]]
    assert call_scriptlet(script, "pre_remove", env).returncode == 0
    assert fake_bin.calls("systemctl")[-1] == [
        "disable", "--now", "plinius-pdf-printer.path", "plinius-pdf-printer.service"]


# --- plinius-apparmor-load ---

class Loader:
    def __init__(self, tmp_path, fake_bin):
        self.tmp = tmp_path
        self.bin = fake_bin
        self.profiles = tmp_path / "profiles"
        self.profiles.mkdir()
        for name in ("plinius.a", "plinius.b", "plinius.c"):
            (self.profiles / name).write_text(f"profile {name} {{}}\n")
        (self.profiles / "subdir").mkdir()  # only files are profiles
        self.conf = tmp_path / "etc"
        self.conf.mkdir()
        self.cache = tmp_path / "cache"
        self.sec = tmp_path / "security"
        (self.sec / "apparmor").mkdir(parents=True)
        (self.sec / "lsm").write_text("capability,landlock,lockdown,yama,apparmor,bpf\n")
        (self.sec / "apparmor/profiles").write_text(
            "plinius.a (complain)\nlsb_release (enforce)\nplinius.b (enforce)\n")
        self.script = rewritten_copy(AA / "plinius-apparmor-load", tmp_path / "load", {
            "/usr/share/plinius-apparmor/profiles": self.profiles,
            "conf=/etc/plinius-apparmor": f"conf={self.conf}",
            "/var/cache/plinius-apparmor": self.cache,
            "/sys/kernel/security": self.sec,
        }, check_left=("/sys/", "=/etc", "=/var/", "=/usr/"))
        fake_bin.stub("apparmor_parser", 'case "${@: -1}" in *plinius.b) exit 1 ;; esac')

    def run(self, *args):
        return run(["sh", self.script, *args], self.bin.env(isolated=True))

    def loaded(self):
        return [c[-1].rsplit("/", 1)[-1] for c in self.bin.calls("apparmor_parser")]


@pytest.fixture
def loader(tmp_path, fake_bin):
    return Loader(tmp_path, fake_bin)


def test_apparmor_loads_each_profile_and_survives_a_bad_one(loader):
    r = loader.run()
    assert r.returncode == 0  # never fails the boot
    assert loader.loaded() == ["plinius.a", "plinius.b", "plinius.c"]
    assert loader.bin.calls("apparmor_parser")[0] == [
        "--replace", "--write-cache", f"--cache-loc={loader.cache}",
        str(loader.profiles / "plinius.a")]
    assert "plinius.b could not be loaded" in r.stderr and "1 profile(s) not loaded" in r.stderr
    assert loader.cache.is_dir()


def test_apparmor_complain_switch(loader):
    (loader.conf / "complain").touch()
    assert loader.run().returncode == 0
    assert all("--Complain" in c for c in loader.bin.calls("apparmor_parser"))


def test_apparmor_off_switch(loader):
    (loader.conf / "off").touch()
    r = loader.run()
    assert r.returncode == 0 and "nothing loaded" in r.stdout
    assert loader.bin.calls("apparmor_parser") == []


def test_apparmor_inactive_kernel(loader):
    (loader.sec / "lsm").write_text("capability,landlock,yama\n")
    r = loader.run()
    assert r.returncode == 0 and "not active" in r.stdout
    assert loader.bin.calls("apparmor_parser") == []


def test_apparmor_lsm_word_match(loader):
    # "apparmor" has to be a whole entry of the list, not part of a longer name
    (loader.sec / "lsm").write_text("capability,notapparmorx\n")
    loader.run()
    assert loader.bin.calls("apparmor_parser") == []


def test_apparmor_status_lists_only_plinius_profiles(loader):
    r = loader.run("--status")
    assert r.returncode == 0
    assert r.stdout == "plinius.a (complain)\nplinius.b (enforce)\n"
    assert loader.bin.calls("apparmor_parser") == []


def test_apparmor_pre_remove_unloads_every_profile(tmp_path, fake_bin):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "plinius.x").write_text("")
    (profiles / "plinius.y").write_text("")
    fake_bin.stub("apparmor_parser", "exit 1")  # not loaded: still no failure
    script = scriptlet("plinius-apparmor", tmp_path, {
        "/usr/share/plinius-apparmor/profiles": profiles,
        "/usr/lib/plinius-apparmor/plinius-apparmor-load": fake_bin.stub("load"),
    })
    env = fake_bin.env(isolated=True)
    assert call_scriptlet(script, "pre_remove", env).returncode == 0
    assert fake_bin.calls("apparmor_parser") == [["--remove", str(profiles / "plinius.x")],
                                                 ["--remove", str(profiles / "plinius.y")]]
    fake_bin.stub("systemctl")
    fake_bin.stub("load", "exit 1")
    assert call_scriptlet(script, "post_upgrade", env).returncode == 0
    assert fake_bin.calls("load") == [[]]


# --- the expected list and the profiles of the package ---

def pkgbuild_arrays(*names):
    # Sourcing a PKGBUILD only sets variables and defines functions
    code = f'source "{AA / "PKGBUILD"}"; ' + "; ".join(
        f'printf "%s " "${{{n}[@]}}"; echo' for n in names)
    out = subprocess.run(["bash", "-c", code], capture_output=True, text=True, check=True,
                         env={"PATH": "/usr/bin:/bin"}).stdout
    return [line.split() for line in out.splitlines()]


def test_expected_lists_exactly_the_packaged_profiles():
    profiles, = pkgbuild_arrays("_profiles")
    expected = (AA / "expected").read_text().split()
    assert expected == sorted(expected) and len(set(expected)) == len(expected)
    assert expected == sorted(f"plinius.{p}" for p in profiles)


@pytest.mark.parametrize("name", (AA / "expected").read_text().split())
def test_profile_file_declares_its_own_name(name):
    text = (AA / name).read_text()
    declared = re.findall(r"^profile\s+(\S+)", text, re.M)
    assert declared[:1] == [name]


def test_every_dropin_points_at_a_packaged_profile():
    profiles, units, user_units = pkgbuild_arrays("_profiles", "_units", "_user_units")
    for entry in units + user_units:
        unit, profile = entry.split(":")
        assert profile in profiles, entry
