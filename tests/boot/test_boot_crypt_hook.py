# The initramfs side of plinius-crypt: the mkinitcpio runtime hook (also sourced by halium-unlock in
# the phone's Halium ramdisk), its install hook, and halium-unlock. These decide whether a root is
# opened, encrypted in place or left alone, and with which sizes cryptsetup and resize2fs run.
# They run as copies with every fixed path pointed into tmp_path; cryptsetup, mount, e2fsck,
# resize2fs, blkdiscard, unl0kr and plinius-progress are stubs that act on files in tmp_path.

import os
import re
import subprocess
import time

import pytest

from conftest import PKG, bash_functions

HOOK = PKG / "plinius-crypt" / "hook"
UNLOCK = PKG / "plinius-crypt" / "halium-unlock"
INSTALL = PKG / "plinius-crypt" / "install"
HEADER = 32 * 1024 * 1024


def test_ramdisk_package_carries_the_same_scripts():
    # plinius-crypt-ramdisk packs these into init.lz4; a copy that drifted would put another
    # prompt into the phone's ramdisk than into mkinitcpio's
    for name in ("hook", "halium-unlock"):
        assert (PKG / "plinius-crypt-ramdisk" / name).read_bytes() == \
            (PKG / "plinius-crypt" / name).read_bytes()


def check_paths(text, allowed):
    """Fail if the code (comments aside) still names a system path not in allowed."""
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    left = set(re.findall(r'(?<![\w.$}/-])/(?:dev|proc|sys|run|var|usr|etc|lib|sbin|bin|plinius)'
                          r'\b[^\s"\')|;<>]*', code))
    assert left <= allowed, left - allowed


def hook_copy(t):
    text = HOOK.read_text()
    for old, new in (("/run/plinius-crypt", t / "run/plinius-crypt"),
                     ("PLINIUS_CRYPT_MNT=/plinius-crypt", f"PLINIUS_CRYPT_MNT={t}/mnt"),
                     ("/etc/plinius-crypt.l10n", t / "l10n"),
                     ("/usr/lib/plinius-security/plinius-pwrate", t / "pwrate"),
                     ("/sys/class/block/", f"{t}/sys/class/block/"),
                     ("/dev/console", t / "console")):
        text = text.replace(old, str(new))
    check_paths(text, {"/dev/null", "/dev/mapper/$PLINIUS_CRYPT_NAME"})
    f = t / "hook"
    f.write_text(text)
    return f


class Crypt:
    """Stubs for the programs of the hook, with their state in files below t/state."""

    def __init__(self, t, fake_bin):
        self.t, self.fb = t, fake_bin
        self.state = t / "state"
        self.state.mkdir()
        (t / "console").write_text("")
        s = self.state
        fake_bin.stub("cryptsetup", f'''
case $1 in
  --version) echo "cryptsetup 2.7.5"; exit 0 ;;
  isLuks) [ -e "{s}/luks" ]; exit ;;
  luksDump) echo "LUKS header information"; [ -e "{s}/reencrypt" ] && echo "Requirements: online-reencrypt"; exit 0 ;;
  open)
    key=$(cat)
    [ -e "{s}/luks" ] && [ "$key" = "$(cat "{s}/password")" ] || exit 2
    exit 0 ;;
  reencrypt)
    cat > "{s}/reencrypt-key"
    echo '{{"progress": 1}}'
    if [ -e "{s}/reencrypt-fails" ]; then command rm -f "{s}/reencrypt-fails"; touch "{s}/luks" "{s}/reencrypt"; exit 1; fi
    touch "{s}/luks"; command rm -f "{s}/reencrypt"
    [ -s "{s}/password" ] || cp "{s}/reencrypt-key" "{s}/password"
    exit 0 ;;
esac
exit 1''')
        # One answer per call, from the top of the file; none left: kill the whole test shell
        # (the hook asks again and again, by design)
        fake_bin.stub("unl0kr", f'''
if [ ! -s "{s}/answers" ]; then echo exhausted > "{s}/exhausted"; kill -KILL 0; fi
head -n 1 "{s}/answers" | tr -d '\\n'
sed -i 1d "{s}/answers"''')
        fake_bin.stub("plinius-progress", f'''
for a in "$@"; do
  [ "$a" = -p ] && {{ cat > /dev/null; exit 0; }}
  [ "$a" = -B ] && {{ c=$(head -n 1 "{s}/choices" 2>/dev/null); sed -i 1d "{s}/choices" 2>/dev/null; exit "${{c:-0}}"; }}
done
exit 0''')
        fake_bin.stub("e2fsck", f'exit "$(cat "{s}/e2fsck-rc" 2>/dev/null || echo 0)"')
        fake_bin.stub("resize2fs", f'''
[ "$1" = -M ] || exit 0
if [ -e "{s}/resize-out" ]; then cat "{s}/resize-out"; else
  echo "Resizing the filesystem on $2 to 12345 (4k) blocks."
  echo "The filesystem on $2 is now 12345 (4k) blocks long."
fi''')
        for name in ("mount", "umount", "blkdiscard", "modprobe", "stty", "sleep"):
            fake_bin.stub(name)
        self.mnt = t / "mnt"
        (t / "sys/class/block/fake-root").mkdir(parents=True)
        # 1 GiB in 512-byte sectors
        (t / "sys/class/block/fake-root/size").write_text("2097152\n")

    def luks(self, password):
        (self.state / "luks").touch()
        (self.state / "password").write_text(password)

    def request(self, text="2026-11-14T10:00:00+01:00"):
        req = self.mnt / "var/lib/plinius-crypt/request"
        req.parent.mkdir(parents=True, exist_ok=True)
        req.write_text(text + "\n")
        return req

    def answers(self, *lines):
        (self.state / "answers").write_text("".join(f"{a}\n" for a in lines))

    def choices(self, *codes):
        (self.state / "choices").write_text("".join(f"{c}\n" for c in codes))

    def run_hook(self, root="/dev/fake-root"):
        hook = hook_copy(self.t)
        code = ('msg() { echo "MSG: $*" >&2; }\nerr() { echo "ERR: $*" >&2; }\n'
                'resolve_device() { echo "$1"; }\n'
                f'. "{hook}"\nroot={root}\nrun_hook\necho "ROOT=$root"\n')
        r = subprocess.run(["bash", "-c", code], env=self.fb.env(isolated=True),
                           capture_output=True, text=True, timeout=20, start_new_session=True)
        assert not (self.state / "exhausted").exists(), "the hook asked more often than expected"
        self.root = re.search(r"^ROOT=(.*)$", r.stdout, re.M).group(1)
        self.err = r.stderr
        return r

    def calls(self, name, first=None):
        return [c for c in self.fb.calls(name) if first is None or c[:1] == [first]]

    def progress_titles(self):
        out = []
        for c in self.fb.calls("plinius-progress"):
            if "-t" in c:
                out.append(c[c.index("-t") + 1])
        return out


@pytest.fixture
def crypt(tmp_path, fake_bin):
    return Crypt(tmp_path, fake_bin)


def test_plain_root_without_request_is_left_alone(crypt):
    crypt.run_hook()
    assert crypt.root == "/dev/fake-root"
    assert [c[0] for c in crypt.calls("cryptsetup")] == ["isLuks"]
    assert crypt.calls("unl0kr") == [] and crypt.calls("e2fsck") == []
    # The marker that tells plinius-encrypt this initramfs runs the hook
    assert (crypt.t / "run/plinius-crypt").is_dir()
    assert [c[:2] for c in crypt.calls("mount")] == [["-o", "ro"]]


def test_luks_root_is_opened_after_a_wrong_password(crypt):
    crypt.luks("right horse")
    crypt.answers("wrong", "right horse")
    crypt.run_hook()
    assert crypt.root == "/dev/mapper/plinius-root"
    opens = crypt.calls("cryptsetup", "open")
    assert [c[1:] for c in opens] == [["--test-passphrase", "--key-file", "-", "/dev/fake-root"]] * 2 \
        + [["--key-file", "-", "/dev/fake-root", "plinius-root"]]
    # The second prompt says why it asks again; the password is never an argument
    second = crypt.calls("unl0kr")[1]
    assert "Wrong password." in second[second.index("-m") + 1]
    assert not any("right horse" in a for c in crypt.fb.calls("cryptsetup") for a in c)
    assert crypt.calls("cryptsetup", "reencrypt") == []


def test_interrupted_encryption_is_resumed_before_opening(crypt):
    crypt.luks("pw")
    (crypt.state / "reencrypt").touch()
    crypt.answers("pw")
    crypt.run_hook()
    re_calls = crypt.calls("cryptsetup", "reencrypt")
    assert len(re_calls) == 1 and "--resume-only" in re_calls[0]
    assert (crypt.state / "reencrypt-key").read_text() == "pw"
    assert crypt.root == "/dev/mapper/plinius-root"
    assert "Resuming the encryption" in crypt.progress_titles()


def test_request_declined_removes_it(crypt):
    req = crypt.request()
    crypt.choices(2)
    crypt.run_hook()
    assert not req.exists()
    assert crypt.root == "/dev/fake-root"
    assert crypt.calls("cryptsetup", "reencrypt") == [] and crypt.calls("e2fsck") == []
    # Removing needs it mounted writable
    assert ["/dev/fake-root", str(crypt.mnt)] in crypt.fb.calls("mount")


def test_request_encrypts_the_whole_device(crypt):
    req = crypt.request()
    crypt.choices(0)
    crypt.answers("new secret", "typo", "new secret", "new secret")
    crypt.run_hook()
    assert crypt.root == "/dev/mapper/plinius-root"
    # Checked, shrunk by the header room, then encrypted in place with the password from stdin
    assert crypt.calls("e2fsck") == [["-f", "-p", "/dev/fake-root"]]
    assert crypt.calls("resize2fs")[0] == ["/dev/fake-root", f"{(1 << 30) // 1024 - 32 * 1024}K"]
    (re_call,) = crypt.calls("cryptsetup", "reencrypt")
    assert re_call[:7] == ["reencrypt", "--encrypt", "--type", "luks2", "--reduce-device-size",
                           "32M", "--progress-json"]
    assert "--device-size" not in re_call and re_call[-1] == "/dev/fake-root"
    assert (crypt.state / "reencrypt-key").read_text() == "new secret"
    assert crypt.calls("blkdiscard") == []
    assert not req.exists()
    assert "Encryption complete" in crypt.progress_titles()
    # The mismatch was explained at the next prompt
    msgs = [c[c.index("-m") + 1] for c in crypt.calls("unl0kr")]
    assert any("do not match" in m for m in msgs[2:])


def test_fresh_device_encrypts_only_the_part_in_use(crypt):
    crypt.request("fresh")
    crypt.choices(0)
    crypt.answers("pw", "pw")
    crypt.run_hook()
    used = 12345 * 4096
    assert crypt.calls("resize2fs")[0] == ["-M", "/dev/fake-root"]
    assert crypt.calls("blkdiscard") == [["-o", str(used), "/dev/fake-root"]]
    (re_call,) = crypt.calls("cryptsetup", "reencrypt")
    i = re_call.index("--device-size")
    assert re_call[i + 1] == str(used + HEADER)
    # The filesystem grows over the opened device again
    assert crypt.calls("resize2fs")[-1] == ["/dev/mapper/plinius-root"]
    assert crypt.root == "/dev/mapper/plinius-root"


def test_filesystem_errors_stop_the_encryption(crypt):
    req = crypt.request()
    crypt.choices(0)
    crypt.answers("pw", "pw")
    (crypt.state / "e2fsck-rc").write_text("4")
    crypt.run_hook()
    assert crypt.calls("resize2fs") == [] and crypt.calls("cryptsetup", "reencrypt") == []
    assert crypt.root == "/dev/fake-root" and req.exists()
    assert "Encryption failed" in crypt.progress_titles()


def test_e2fsck_corrections_do_not_stop_it(crypt):
    crypt.request()
    crypt.choices(0)
    crypt.answers("pw", "pw")
    (crypt.state / "e2fsck-rc").write_text("1")  # errors corrected
    crypt.run_hook()
    assert len(crypt.calls("cryptsetup", "reencrypt")) == 1


def test_unexpected_resize2fs_output_stops_a_fresh_encryption(crypt):
    req = crypt.request("fresh")
    crypt.choices(0)
    crypt.answers("pw", "pw")
    (crypt.state / "resize-out").write_text("resize2fs: Device or resource busy\n")
    crypt.run_hook()
    assert crypt.calls("blkdiscard") == [] and crypt.calls("cryptsetup", "reencrypt") == []
    assert crypt.root == "/dev/fake-root" and req.exists()


def test_no_matching_password_after_five_rounds(crypt):
    req = crypt.request()
    crypt.choices(0)
    crypt.answers(*[x for i in range(5) for x in (f"a{i}", f"b{i}")])
    crypt.run_hook()
    assert len(crypt.calls("unl0kr")) == 10
    assert crypt.calls("cryptsetup", "reencrypt") == []
    assert crypt.root == "/dev/fake-root" and req.exists()
    assert "Encryption not set up" in crypt.progress_titles()


def test_empty_password_goes_back_to_the_choice(crypt):
    req = crypt.request()
    crypt.choices(0, 2)
    crypt.answers("")
    crypt.run_hook()
    assert crypt.calls("cryptsetup", "reencrypt") == []
    assert not req.exists() and crypt.root == "/dev/fake-root"


def test_interrupted_encryption_asks_to_unlock_and_resumes(crypt):
    crypt.request()
    crypt.choices(0)
    (crypt.state / "reencrypt-fails").touch()
    (crypt.state / "password").write_text("pw")
    crypt.answers("pw", "pw", "pw")
    crypt.run_hook()
    assert "Encryption interrupted" in crypt.progress_titles()
    calls = crypt.calls("cryptsetup", "reencrypt")
    assert "--encrypt" in calls[0] and "--resume-only" in calls[1]
    assert crypt.root == "/dev/mapper/plinius-root"


def test_tr_msg_translates_from_the_table(tmp_path):
    table = tmp_path / "l10n"
    table.write_text("Password to unlock\tPasswort zum Entsperren\nYes\tJa\n")
    code = bash_functions(HOOK, "tr_msg") + (
        f'PLINIUS_CRYPT_L10N={table}\n'
        'tr_msg "Password to unlock"; echo; tr_msg "Untranslated"; echo; tr_msg "Ye"; echo\n'
        f'PLINIUS_CRYPT_L10N={tmp_path}/missing; tr_msg "Yes"; echo\n')
    r = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
    assert r.stdout == "Passwort zum Entsperren\nUntranslated\nYe\nYes\n"


# --- install hook --------------------------------------------------------------------------------

def test_install_adds_what_the_hook_runs(tmp_path):
    look = tmp_path / "look-files"
    look.write_text('#!/bin/sh\necho "look-files $1" >> "$LOG"\n')
    look.chmod(0o755)
    text = INSTALL.read_text().replace("/usr/lib/plinius-crypt/look-files", str(look))
    log = tmp_path / "log"
    code = (
        'map() { local f=$1; shift; for a in "$@"; do "$f" "$a"; done; }\n'
        'for f in add_module add_all_modules add_udev_rule add_binary add_file add_full_dir '
        'add_runscript error; do eval "$f() { echo \\"$f \\$*\\" >> \\"\\$LOG\\"; }"; done\n'
        + text + '\nBUILDROOT=/buildroot build\n')
    r = subprocess.run(["bash", "-c", code], env={"LOG": str(log), "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lines = log.read_text().splitlines()
    binaries = {l.split()[1] for l in lines if l.startswith("add_binary ")}
    # Every program the runtime hook calls that busybox does not have
    for prog in ("cryptsetup", "unl0kr", "plinius-progress", "e2fsck", "resize2fs", "blkdiscard"):
        assert prog in binaries
        assert re.search(rf"\b{prog}\b", HOOK.read_text())
    assert "add_module dm-crypt?" in lines and "add_runscript " in lines
    assert "add_file /usr/share/plinius-crypt/glass-background.bin" in lines
    assert "look-files /buildroot" in lines
    assert not any(l.startswith("error") for l in lines)


def test_make_parts_carries_the_same_programs():
    # The Halium ramdisk runs the same hook: it needs the same programs
    text = (PKG / "plinius-crypt-ramdisk/make-parts").read_text()
    block = text[text.index("programs = {"):text.index("}", text.index("programs = {"))]
    for prog in ("cryptsetup", "unl0kr", "plinius-progress", "e2fsck", "resize2fs", "blkdiscard",
                 "plinius-pwrate"):
        assert f'"{prog}"' in block


# --- halium-unlock -------------------------------------------------------------------------------

def unlock_copy(t):
    plinius = t / "plinius"
    (plinius / "bin").mkdir(parents=True)
    hook_copy(t).rename(plinius / "hook")
    text = UNLOCK.read_text()
    for old, new in (("/plinius/", f"{plinius}/"), ("ls /plinius ", f"ls {plinius} "), ("/run/", f"{t}/run/"),
                     ("/dev/kmsg", t / "kmsg"), ("/sys/", f"{t}/sys/"), ("/proc/", f"{t}/proc/"),
                     ("/sbin/insmod", "insmod"), ("/lib/modules/", f"{t}/lib/modules/"),
                     ("/etc/unl0kr.conf.d/", f"{t}/unl0kr.conf.d/")):
        text = text.replace(old, str(new))
    # /dev/kmsg keeps every line; a plain file only does when appended to
    text = text.replace(f"> {t}/kmsg", f">> {t}/kmsg")
    check_paths(text, {"/dev/null", "/bin/sh"})
    f = t / "unlock"
    f.write_text(text)
    return f


@pytest.fixture
def unlock(crypt):
    t = crypt.t
    f = unlock_copy(t)
    # The programs of vendor_boot are there
    (t / "plinius/bin/cryptsetup").symlink_to(crypt.fb.path / "cryptsetup")
    for name in ("insmod", "udevadm", "uname", "dmesg"):
        crypt.fb.stub(name)
    (t / "proc/bus/input").mkdir(parents=True)
    (t / "proc/bus/input/devices").write_text('I: Bus=0018\nN: Name="fts_ts"\n')
    level = t / "sys/bus/spi/drivers/fts_ts/spi1.0/fts_log_level"
    level.parent.mkdir(parents=True)
    level.write_text("1\n")
    bl = t / "sys/class/leds/lcd-backlight"
    bl.mkdir(parents=True)
    (bl / "max_brightness").write_text("2047\n")
    (bl / "brightness").write_text("0\n")
    (t / "kmsg").write_text("")

    def run():
        r = subprocess.run(["bash", str(f), "/dev/sda32"], env=crypt.fb.env(isolated=True),
                           capture_output=True, text=True, timeout=20, start_new_session=True)
        assert not (crypt.state / "exhausted").exists()
        # The boot logo runs in the background: wait for its stub to finish (it exits at once)
        pid_file = t / "run/plinius-crypt/logo.pid"
        if pid_file.exists():
            pid, deadline = int(pid_file.read_text()), time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.005)
        return r
    return run


def test_unlock_plain_partition_stays_quiet(crypt, unlock):
    r = unlock()
    assert r.returncode == 0
    kmsg = (crypt.t / "kmsg").read_text()
    assert "/dev/sda32: plain, no request" in kmsg
    assert crypt.fb.calls("insmod") == [] and crypt.calls("unl0kr") == []
    assert not (crypt.t / "run/plinius-crypt/data-dev").exists()


def test_unlock_opens_the_data_partition(crypt, unlock):
    crypt.luks("pw")
    crypt.answers("pw")
    (crypt.t / "plinius/share/boot-logo").mkdir(parents=True)
    (crypt.t / "plinius/share/boot-logo/mark.bin").write_bytes(b"logo")
    r = unlock()
    assert r.returncode == 0, r.stderr
    assert (crypt.t / "run/plinius-crypt/data-dev").read_text() == "/dev/mapper/plinius-data\n"
    (open_call,) = [c for c in crypt.calls("cryptsetup", "open") if "--test-passphrase" not in c]
    assert open_call[-2:] == ["/dev/sda32", "plinius-data"]
    # The touch driver logs no coordinates from here on (they would give the password away)
    assert (crypt.t / "sys/bus/spi/drivers/fts_ts/spi1.0/fts_log_level").read_text() == "0\n"
    assert (crypt.t / "sys/class/leds/lcd-backlight/brightness").read_text() == "1023\n"
    assert crypt.fb.calls("sleep") == []
    # The boot logo, and how the system ends it
    assert any("-l" in c for c in crypt.fb.calls("plinius-progress"))
    quit_script = crypt.t / "run/plinius-crypt/logo-quit"
    assert subprocess.run(["bash", "-n", str(quit_script)]).returncode == 0
    dropin = crypt.t / "run/systemd/system/user@.service.d/plinius-crypt-logo.conf"
    assert f"ExecStartPost=+-/bin/sh {quit_script}" in dropin.read_text()


def test_unlock_request_on_the_data_partition(crypt, unlock):
    # The request lies at the partition's top level, where plinius-encrypt puts it
    req = crypt.mnt / ".plinius-crypt-request"
    req.parent.mkdir(parents=True)
    req.write_text("2026-11-14\n")
    crypt.choices(2)
    r = unlock()
    assert r.returncode == 0, r.stderr
    assert not req.exists()
    assert not (crypt.t / "run/plinius-crypt/data-dev").exists()


def language_of_the_installation(crypt):
    """What Plinius Boot leaves next to its request: the table of the chosen language and the keyboard."""
    crypt.mnt.mkdir(parents=True, exist_ok=True)
    (crypt.mnt / ".plinius-crypt-l10n").write_text("Encrypt the device?\tGerät verschlüsseln?\n")
    (crypt.mnt / ".plinius-crypt-keyboard").write_text("[keyboard]\nlayout=de\n")


def test_unlock_first_start_speaks_the_language_of_the_installation(crypt, unlock):
    language_of_the_installation(crypt)
    (crypt.mnt / ".plinius-crypt-request").write_text("fresh\n")
    crypt.choices(2)
    r = unlock()
    assert r.returncode == 0, r.stderr
    assert "Gerät verschlüsseln?" in crypt.progress_titles()
    assert (crypt.t / "unl0kr.conf.d/40-plinius-keyboard.conf").read_text() == "[keyboard]\nlayout=de\n"
    assert "language of the installation" in (crypt.t / "kmsg").read_text()


def test_unlock_encrypted_partition_keeps_the_language_of_its_ramdisk(crypt, unlock):
    language_of_the_installation(crypt)
    crypt.luks("pw")
    crypt.answers("pw")
    r = unlock()
    assert r.returncode == 0, r.stderr
    assert not (crypt.t / "l10n").exists() and not (crypt.t / "unl0kr.conf.d").exists()


def test_unlock_reports_missing_programs(crypt, unlock):
    (crypt.t / "plinius/bin/cryptsetup").unlink()
    unlock()
    assert "the programs of vendor_boot are missing" in (crypt.t / "kmsg").read_text()


# --- names shared between the scripts ------------------------------------------------------------

def test_request_and_mapping_names_agree():
    encrypt = (PKG / "plinius-crypt/plinius-encrypt").read_text()
    hook = HOOK.read_text()
    unlock = UNLOCK.read_text()
    pam = (PKG / "plinius-crypt/pam_plinius_crypt.c").read_text()
    assert "req=/var/lib/plinius-crypt/request" in encrypt
    assert "PLINIUS_CRYPT_REQUEST=var/lib/plinius-crypt/request" in hook
    assert "req=/userdata/.plinius-crypt-request" in encrypt
    assert "PLINIUS_CRYPT_REQUEST=.plinius-crypt-request" in unlock
    assert "mapper=plinius-root" in encrypt and "PLINIUS_CRYPT_NAME=plinius-root" in hook
    assert "mapper=plinius-data" in encrypt and "PLINIUS_CRYPT_NAME=plinius-data" in unlock
    assert '"plinius-root", "plinius-data"' in pam
    # The marker plinius-encrypt looks for is made by the hook
    assert "hook_ran=/run/plinius-crypt" in encrypt and "mkdir -p /run/plinius-crypt" in hook
