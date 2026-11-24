# plinius-encrypt (request, cancel, status, change of the encryption password) and
# rebuild-initramfs (when the initramfs or the Halium ramdisk is rebuilt). Both run as copies with
# their fixed paths pointed into tmp_path; cryptsetup, findmnt, mkinitcpio and the helpers are stubs.

import os
import re
import subprocess
import time

import pytest

from conftest import PKG

ENCRYPT = PKG / "plinius-crypt" / "plinius-encrypt"
REBUILD = PKG / "plinius-crypt" / "rebuild-initramfs"


def check_paths(text, allowed):
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    left = set(re.findall(r'(?<![\w.$}/-])/(?:dev|proc|sys|run|var|usr|etc|userdata)\b[^\s"\')|;<>]*',
                          code))
    assert left <= allowed, left - allowed


class Encrypt:
    def __init__(self, t, fb):
        self.t, self.fb = t, fb
        self.state = t / "state"
        self.state.mkdir()
        text = ENCRYPT.read_text()
        for old, new in (("/usr/share/buffybox/unl0kr-keycaps", t / "keycaps"),
                         ("/usr/lib/plinius-crypt/login-password-check", t / "login-check"),
                         ("/usr/lib/plinius-security/plinius-pwrate", t / "pwrate"),
                         ("/var/lib/plinius-crypt/", f"{t}/var/lib/plinius-crypt/"),
                         ("/userdata/.plinius-crypt-request", t / "userdata/.plinius-crypt-request"),
                         ("hook_ran=/run/plinius-crypt", f"hook_ran={t}/run/plinius-crypt"),
                         ("$EUID", "${FAKE_EUID:?}")):
            text = text.replace(old, str(new))
        # findmnt's arguments keep naming /userdata (only asked, never written)
        check_paths(text, {"/userdata", "/dev/mapper/*", "/dev/null"})
        self.script = t / "plinius-encrypt"
        self.script.write_text(text)
        # The keys of unl0kr's keyboard, one per line (a small set for the tests)
        (t / "keycaps").write_text("\n".join(list("abcdefghijklmnopqrstuvwxyz0123456789 ") + ["ä", "!"])
                                   + "\n")
        s = self.state
        fb.stub("findmnt", f'''
[ -e "{s}/halium" ] || [ "$2" != /userdata ] || [ "$#" -gt 2 ] || exit 1
[ "$1 $2" = "-n /userdata" ] && exit 0
case "$*" in
  *SOURCE*) cat "{s}/source" ;;
  *FSTYPE*) cat "{s}/fstype" ;;
esac''')
        fb.stub("cryptsetup", f'''
case $1 in
  status) [ -e "{s}/encrypted" ] && printf '  type:    LUKS2\\n  device:  /dev/sda32\\n'; exit 0 ;;
  open) [ "$(cat "$4")" = "$(cat "{s}/password")" ]; exit ;;
  luksChangeKey) cat "$3" > "{s}/old-given"; cat "$5" > "{s}/new-given"; exit "$(cat "{s}/change-rc" 2>/dev/null || echo 0)" ;;
esac
exit 1''')
        fb.stub("sync")
        write_exec(t / "login-check", f'#!/bin/sh\ncat > "{s}/pin-asked"\n'
                                      f'exit "$(cat "{s}/pin-rc" 2>/dev/null || echo 1)"\n')
        write_exec(t / "pwrate", '#!/bin/sh\nread -r p\necho "level=${#p}"\n')
        self.source("/dev/sda32", "ext4")

    def source(self, dev, fs):
        (self.state / "source").write_text(dev + "\n")
        (self.state / "fstype").write_text(fs + "\n")

    def run(self, *args, stdin="", root=True):
        env = self.fb.env(isolated=True, FAKE_EUID=0 if root else 1000)
        return subprocess.run(["bash", str(self.script), *args], input=stdin, env=env,
                              capture_output=True, text=True, timeout=20)

    def hook_ran(self):
        (self.t / "run/plinius-crypt").mkdir(parents=True, exist_ok=True)


def write_exec(path, text):
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def enc(tmp_path, fake_bin):
    return Encrypt(tmp_path, fake_bin)


# --- status --------------------------------------------------------------------------------------

def test_status_words(enc):
    assert enc.run("--status", root=False).stdout == "unsupported\n"  # the hook did not run
    enc.hook_ran()
    assert enc.run("--status", root=False).stdout == "off\n"
    req = enc.t / "var/lib/plinius-crypt/request"
    req.parent.mkdir(parents=True)
    req.write_text("x")
    assert enc.run("--status", root=False).stdout == "requested\n"
    enc.source("/dev/sda32", "btrfs")
    assert enc.run("--status", root=False).stdout == "unsupported\n"
    enc.source("/dev/mapper/plinius-root", "ext4")
    assert enc.run("--status", root=False).stdout == "encrypted\n"


def test_status_on_the_halium_system_looks_at_userdata(enc):
    (enc.state / "halium").touch()
    enc.hook_ran()
    assert enc.run("--status").stdout == "off\n"
    req = enc.t / "userdata/.plinius-crypt-request"
    req.parent.mkdir()
    req.write_text("x")
    assert enc.run("--status").stdout == "requested\n"


# --- request and cancel --------------------------------------------------------------------------

def test_request_writes_the_request_file(enc):
    enc.hook_ran()
    r = enc.run("--request")
    assert r.returncode == 0, r.stderr
    req = enc.t / "var/lib/plinius-crypt/request"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d\n", req.read_text())
    # Plain request, never "fresh": that one only comes from the image of a new device
    assert "fresh" not in req.read_text()
    r = enc.run("--cancel")
    assert r.returncode == 0 and not req.exists()


def test_request_on_the_halium_system(enc):
    (enc.state / "halium").touch()
    enc.hook_ran()
    assert enc.run().returncode == 0
    assert (enc.t / "userdata/.plinius-crypt-request").exists()
    assert not (enc.t / "var/lib/plinius-crypt/request").exists()


@pytest.mark.parametrize("setup,code,text", [
    (lambda e: None, 7, "initramfs does not contain the plinius-crypt hook"),
    (lambda e: (e.hook_ran(), e.source("/dev/sda32", "f2fs")), 7, "Only ext4"),
    (lambda e: (e.hook_ran(), e.source("/dev/mapper/plinius-root", "ext4")), 0, "already encrypted"),
])
def test_request_refused(enc, setup, code, text):
    setup(enc)
    r = enc.run("--request")
    assert r.returncode == code
    assert text in r.stdout + r.stderr
    assert not (enc.t / "var/lib/plinius-crypt/request").exists()


def test_request_needs_root(enc):
    enc.hook_ran()
    r = enc.run(root=False)
    assert r.returncode == 1 and "as root" in r.stderr
    assert not (enc.t / "var/lib/plinius-crypt").exists()
    assert enc.run("--cancel", root=False).returncode == 1


@pytest.mark.parametrize("args", [["--bogus"], ["--status", "--stdin"], ["--request", "--cancel"],
                                  ["--change-password", "--stdin", "x"], ["--stdin"]])
def test_usage_errors(enc, args):
    r = enc.run(*args)
    assert r.returncode == 2 and "Usage" in r.stderr


# --- change of the password ----------------------------------------------------------------------

@pytest.fixture
def encrypted(enc):
    (enc.state / "encrypted").touch()
    (enc.state / "password").write_text("old pass")
    return enc


def test_change_password(encrypted):
    r = encrypted.run("--change-password", "--stdin", stdin="old pass\nnew päss!")
    assert r.returncode == 0, r.stderr
    assert (encrypted.state / "old-given").read_text() == "old pass"
    assert (encrypted.state / "new-given").read_text() == "new päss!"
    # Compared with the PIN through stdin, rated, and never on a command line
    assert (encrypted.state / "pin-asked").read_text() == "new päss!\n"
    assert (encrypted.t / "var/lib/plinius-crypt/strength").read_text() == "level=9\n"
    for c in encrypted.fb.calls("cryptsetup"):
        assert not any(secret in a for a in c for secret in ("old pass", "new päss!"))


def test_change_password_not_encrypted(enc):
    r = enc.run("--change-password", "--stdin", stdin="a\nb\n")
    assert r.returncode == 6


def test_change_password_wants_two_lines(encrypted):
    assert encrypted.run("--change-password", "--stdin", stdin="only one\n").returncode == 2
    assert not (encrypted.state / "new-given").exists()


@pytest.mark.parametrize("new,bad", [("", None), ("abc€", "€"), ("ABC", "ABC"), ("a\tb", "\t")])
def test_change_password_refuses_untypeable(encrypted, new, bad):
    r = encrypted.run("--change-password", "--stdin", stdin=f"old pass\n{new}\n")
    assert r.returncode == 4
    if bad:
        assert r.stderr.rstrip("\n").endswith(bad)
    assert not (encrypted.state / "new-given").exists()
    # The cheap check comes first: no key derivation, no comparison with the PIN
    assert [c for c in encrypted.fb.calls("cryptsetup") if c[0] == "open"] == []
    assert not (encrypted.state / "pin-asked").exists()


def test_change_password_wrong_current(encrypted):
    r = encrypted.run("--change-password", "--stdin", stdin="wrong\nnew pass\n")
    assert r.returncode == 3
    # Whether a text is someone's PIN is only told after the current password was right
    assert not (encrypted.state / "pin-asked").exists()
    assert not (encrypted.state / "new-given").exists()


@pytest.mark.parametrize("pin_rc,code", [(0, 5), (2, 1)])
def test_change_password_pin_check(encrypted, pin_rc, code):
    (encrypted.state / "pin-rc").write_text(str(pin_rc))
    r = encrypted.run("--change-password", "--stdin", stdin="old pass\n1234\n")
    assert r.returncode == code
    assert not (encrypted.state / "new-given").exists()


def test_change_password_cryptsetup_fails(encrypted):
    (encrypted.state / "change-rc").write_text("2")
    r = encrypted.run("--change-password", "--stdin", stdin="old pass\nnew pass\n")
    assert r.returncode == 1
    assert not (encrypted.t / "var/lib/plinius-crypt/strength").exists()


def test_change_password_needs_root(encrypted):
    r = encrypted.run("--change-password", "--stdin", stdin="old pass\nnew\n", root=False)
    assert r.returncode == 1 and not (encrypted.state / "new-given").exists()


# --- rebuild-initramfs ---------------------------------------------------------------------------

class Rebuild:
    def __init__(self, t, fb):
        self.t, self.fb = t, fb
        text = REBUILD.read_text()
        for old, new in (("/var/lib/plinius-crypt/", f"{t}/var/lib/plinius-crypt/"),
                         ("/etc/", f"{t}/etc/"),
                         ("/usr/lib/plinius-crypt/halium-rebuild", t / "halium-rebuild")):
            text = text.replace(old, str(new))
        check_paths(text, {"/userdata", "/dev/null"})
        self.script = t / "rebuild-initramfs"
        self.script.write_text(text)
        self.last = t / "var/lib/plinius-crypt/last-rebuild"
        self.look = t / "var/lib/plinius-crypt/look"
        self.look.parent.mkdir(parents=True)
        fb.stub("sleep")
        fb.stub("findmnt", "exit 1")
        fb.stub("mkinitcpio", '[ -e "$CHANGE_ONCE" ] && { command rm -f "$CHANGE_ONCE"; '
                              'echo changed >> "$LOOK"; }; [ -n "$ALWAYS" ] && echo x >> "$LOOK"; exit 0')
        write_exec(t / "halium-rebuild", f'#!/bin/sh\necho run >> "{t}/halium.log"\n')

    def run(self, **env):
        e = self.fb.env(isolated=True, LOOK=self.look, CHANGE_ONCE=self.t / "change-once", **env)
        r = subprocess.run(["bash", str(self.script)], env=e, capture_output=True, text=True,
                           timeout=20)
        assert r.returncode == 0, r.stderr
        return r

    def sleeps(self):
        return [int(c[0]) for c in self.fb.calls("sleep")]


@pytest.fixture
def reb(tmp_path, fake_bin):
    return Rebuild(tmp_path, fake_bin)


def test_rebuild_first_time(reb):
    reb.run()
    assert reb.fb.calls("mkinitcpio") == [["-P"]]
    assert reb.sleeps() == [3]
    assert reb.last.exists()


def test_rebuild_waits_for_the_interval(reb):
    reb.last.touch()
    t = time.time() - 100
    os.utime(reb.last, (t, t))
    reb.run()
    first, wait = reb.sleeps()
    assert first == 3 and 195 <= wait <= 200
    assert len(reb.fb.calls("mkinitcpio")) == 1


def test_rebuild_after_the_clock_went_back_waits_no_longer_than_the_interval(reb):
    reb.last.touch()
    t = time.time() + 10 * 86400
    os.utime(reb.last, (t, t))
    reb.run()
    assert reb.sleeps() == [3, 300]


def test_rebuild_again_when_settings_changed_meanwhile(reb):
    (reb.t / "change-once").touch()
    reb.run()
    assert len(reb.fb.calls("mkinitcpio")) == 2
    # Never more than three builds in one run, even if the settings never settle
    (reb.fb.path / "mkinitcpio.log").unlink()
    reb.run(ALWAYS=1)
    assert len(reb.fb.calls("mkinitcpio")) == 3


def test_rebuild_on_the_halium_system(reb):
    (reb.t / "var/lib/plinius-crypt/halium").mkdir()
    (reb.t / "var/lib/plinius-crypt/halium/target").write_text("vendor_boot\n")
    reb.run()
    assert reb.fb.calls("mkinitcpio") == []
    assert (reb.t / "halium.log").read_text() == "run\n"
