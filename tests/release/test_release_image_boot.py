# The boot test of an image: tools/image-boot-report.py (judges the serial log of the check that
# image/test-image-guest.sh prints in the guest) and image/test-image.sh (unpacks the generic
# kernel, hands the check to QEMU, asks the report). QEMU is a stand-in that records how it was
# started and writes a canned serial log; bsdtar, depmod, mke2fs and debugfs are the real ones,
# on a small ext4 image and a stand-in kernel package in tmp_path.

import base64
import io
import re
import shutil
import subprocess
import tarfile

import pytest

from conftest import REPO, TOOLS, load_script

REPORT = TOOLS / "image-boot-report.py"
EXPECTED = REPO / "image" / "test-image.expected"
KVER = "7.3.0-1-aarch64-ARCH"

RULES = """\
# comment line
active dbus.service
active greetd.service   # reason
fails phone-only.service   # needs the phone
restarts loop.service
"""


def log(*lines, end=True, active=("dbus.service", "greetd.service")):
    body = ["@@IMAGE-CHECK begin", "@@IMAGE-CHECK state degraded", f"@@IMAGE-CHECK kernel {KVER}",
            *lines,
            *(f"@@IMAGE-CHECK unit {u} active success" for u in active),
            "@@IMAGE-CHECK pacman-dk ok", "@@IMAGE-CHECK bus ok",
            "@@IMAGE-CHECK time Startup finished in 2s (kernel) + 5min (userspace) = 5min 2s"]
    if end:
        body.append("@@IMAGE-CHECK end")
    return "\n".join(["[    0.0] booting", *body, "[  300.0] reboot: Power down"]) + "\n"


@pytest.fixture
def rep(tmp_path):
    m = load_script(REPORT)
    rules = tmp_path / "rules"
    rules.write_text(RULES)

    def run(text, *extra):
        serial = tmp_path / "serial.log"
        serial.write_text(text)
        report = tmp_path / "report.txt"
        code = m.main([str(serial), str(rules), "--report", str(report), *extra])
        return code, report.read_text() if report.exists() else ""

    m.run = run
    m.rules_file = rules
    return m


# --- image-boot-report.py ----------------------------------------------------------------------

def test_read_rules(rep):
    rules = rep.read_rules(rep.rules_file)
    assert rules == {"active": ["dbus.service", "greetd.service"], "fails": {"phone-only.service"},
                     "restarts": {"loop.service"}}


@pytest.mark.parametrize("line", ["active", "active a b", "wants x.service"])
def test_read_rules_refuses_a_wrong_line(rep, tmp_path, line):
    bad = tmp_path / "bad"
    bad.write_text(f"active ok.service\n{line}\n")
    with pytest.raises(ValueError, match=r"bad:2: expected 'active\|fails\|restarts UNIT'"):
        rep.read_rules(bad)


def test_the_expected_rules_of_the_repository_load(rep):
    rules = rep.read_rules(EXPECTED)
    assert "dbus.service" in rules["active"] and "greetd.service" in rules["active"]
    assert rules["fails"] and rules["restarts"]


def test_a_good_boot_passes(rep, capsys):
    code, report = rep.run(log(
        "@@IMAGE-CHECK failed phone-only.service",
        "@@IMAGE-CHECK why phone-only.service no such device",
        "@@IMAGE-CHECK restarts loop.service 70",
        "@@IMAGE-CHECK blame 4min 11s slow.service",
        "@@IMAGE-CHECK journal unit 12 failed", "@@IMAGE-CHECK journal unit 13 failed",
        "@@IMAGE-CHECK journal other"))
    assert code == 0
    assert "boot test passed (Startup finished in 2s" in capsys.readouterr().err
    assert report.startswith("Verdict: passed\n")
    assert "Failed as expected without the phone:\n  phone-only.service" in report
    assert "Restarts:\n  loop.service: 70" in report
    assert "Slowest units:\n  4min 11s slow.service" in report
    assert "Journal errors (3 lines, by kind):\n     2 unit N failed\n     1 other" in report
    assert f"Kernel: {KVER}" in report and "State: degraded" in report


def test_a_mark_after_other_text_on_the_line_counts(rep):
    text = log().replace("@@IMAGE-CHECK begin", "plinius login: @@IMAGE-CHECK begin")
    assert rep.run(text)[0] == 0


def test_an_unexpected_failure_fails_with_its_reason(rep, capsys):
    code, report = rep.run(log("@@IMAGE-CHECK failed broken.service",
                               "@@IMAGE-CHECK why broken.service Failed to load libfoo.so"))
    assert code == 1
    err = capsys.readouterr().err
    assert "Error: the image failed its boot test:" in err
    assert "  failed: broken.service\n    Failed to load libfoo.so" in err
    assert report.startswith("Verdict: FAILED\n\nProblems:\n  failed: broken.service")
    assert "Allowed to fail, but did not:\n  phone-only.service" in report


def test_an_unexpected_restart_fails(rep):
    code, report = rep.run(log("@@IMAGE-CHECK restarts flapping.service 3",
                               "@@IMAGE-CHECK why flapping.service cannot open /dev/foo",
                               "@@IMAGE-CHECK restarts odd.service x"))
    assert code == 1
    assert "restarted 3 times: flapping.service\n    cannot open /dev/foo" in report
    assert "restarted 0 times: odd.service" in report


def test_a_unit_that_must_run_and_may_restart_can_be_between_two_starts(rep):
    rep.rules_file.write_text(RULES + "restarts greetd.service\n")
    text = log("@@IMAGE-CHECK restarts greetd.service 6").replace(
        "unit greetd.service active success", "unit greetd.service activating success")
    code, report = rep.run(text)
    assert code == 0, report
    # but it still has to come up at all
    code, report = rep.run(text.replace("activating success", "failed exit-code"))
    assert code == 1 and "not active: greetd.service (failed exit-code)" in report


def test_activating_is_not_enough_for_a_unit_that_may_not_restart(rep):
    code, report = rep.run(log().replace("unit greetd.service active success",
                                         "unit greetd.service activating success"))
    assert code == 1 and "not active: greetd.service (activating success)" in report


def test_a_unit_that_must_run_but_does_not(rep):
    text = log().replace("unit greetd.service active success", "unit greetd.service failed exit-code")
    code, report = rep.run(text)
    assert code == 1 and "not active: greetd.service (failed exit-code)" in report
    text = "\n".join(l for l in log().splitlines() if "greetd" not in l)
    code, report = rep.run(text)
    assert code == 1 and "not reported: greetd.service" in report


def test_broken_dependencies_and_bus(rep):
    text = log().replace("pacman-dk ok", "pacman-dk error: foo requires bar").replace(
        "bus ok", "bus broken")
    code, report = rep.run(text)
    assert code == 1
    assert "pacman -Dk: the installed packages' dependencies are broken\n    error: foo requires bar" in report
    assert "the system bus does not answer" in report


def test_a_boot_that_never_reaches_the_check(rep):
    code, report = rep.run("[    0.0] Kernel panic - not syncing: VFS: Unable to mount root fs\n")
    assert code == 1
    assert "the check never started: the boot did not get past graphical.target" in report
    assert "Allowed to fail" not in report


def test_a_check_that_does_not_finish(rep):
    text = "\n".join(l for l in log(end=False).splitlines()
                     if "unit greetd" not in l and "pacman" not in l and "bus" not in l) + "\n"
    code, report = rep.run(text)
    assert code == 1
    assert "the check did not finish (hung or the machine stopped)" in report
    # Without the end, a missing report of a unit, pacman or the bus is no extra problem
    assert "not reported" not in report and "pacman" not in report and "bus" not in report


def test_a_boot_that_does_not_settle(rep):
    text = log("@@IMAGE-CHECK job 12 foo.service start running").replace(
        "state degraded", "state starting")
    code, report = rep.run(text)
    assert code == 1
    assert "system state after the boot: 'starting'\n    job still waiting: 12 foo.service start running" in report


def test_undecodable_bytes_in_the_log(rep, tmp_path):
    serial = tmp_path / "serial.log"
    serial.write_bytes(log().encode() + b"\xff\xfe garbage\n")
    assert rep.main([str(serial), str(rep.rules_file)]) == 0


@pytest.mark.parametrize("args", [[], ["a"], ["a", "b", "c"], ["a", "b", "--report"]])
def test_usage(rep, args, capsys):
    assert rep.main(args) == 2
    assert "usage: tools/image-boot-report.py" in capsys.readouterr().err


# --- image/test-image.sh -----------------------------------------------------------------------

QEMU = r'''
serial= append= drives=()
while (($#)); do
	case $1 in
	-serial) serial=${2#file:}; shift ;;
	-append) append=$2; shift ;;
	-drive) drives+=("$2"); shift ;;
	esac
	shift
done
printf '%s\n' "$append" > "$FAKE_QEMU/append"
printf '%s\n' "${drives[@]}" > "$FAKE_QEMU/drives"
echo "$TMPDIR" > "$FAKE_QEMU/tmpdir"
cat "$FAKE_QEMU/serial" >> "$serial"
exit "${FAKE_QEMU_EXIT:-0}"
'''


class Volla:
    def __init__(self, tmp_path, fake_bin):
        self.root = tmp_path / "volla"
        for d in ("tools", "image", "build/cache/pacman"):
            (self.root / d).mkdir(parents=True)
        shutil.copy2(TOOLS / "lib.sh", self.root / "tools")
        shutil.copy2(REPORT, self.root / "tools")
        for f in ("test-image.sh", "test-image-guest.sh", "test-image.expected"):
            shutil.copy2(REPO / "image" / f, self.root / "image")
        self.qemu = tmp_path / "qemu"
        self.qemu.mkdir()
        self.fake_bin = fake_bin
        fake_bin.stub("qemu-system-aarch64", QEMU)
        for tool in ("debugfs", "mke2fs", "depmod", "du", "uname"):
            found = shutil.which(tool)
            if not found:
                pytest.skip(f"{tool} missing on the host")
            (fake_bin.path / tool).symlink_to(found)
        bsdtar = shutil.which("bsdtar")
        if not bsdtar:
            pytest.skip("bsdtar missing on the host")
        fake_bin.stub("bsdtar", f'echo "$*" >> "{tmp_path}/bsdtar.log"\nexec {bsdtar} "$@"')
        self.tmp = tmp_path
        self.kernel_package()
        self.image = self.ext4(tmp_path / "plinius-arch-test.img")
        self.serial(self.log())

    @staticmethod
    def log(*lines, **kw):
        """A serial log with every unit of image/test-image.expected that must run"""
        return log(*lines, active=load_script(REPORT).read_rules(EXPECTED)["active"], **kw)

    def kernel_package(self, name="linux-aarch64-7.3-1-aarch64.pkg.tar.xz", image=True):
        buf = tarfile.open(self.root / "build/cache/pacman" / name, "w:xz")
        members = {f"usr/lib/modules/{KVER}/modules.builtin": b"kernel/fs/ext4/ext4.ko\n",
                   f"usr/lib/modules/{KVER}/modules.builtin.modinfo": b"",
                   f"usr/lib/modules/{KVER}/modules.order": b""}
        if image:
            members["boot/Image"] = b"ARM64 kernel image"
        for n, data in members.items():
            info = tarfile.TarInfo(n)
            info.size = len(data)
            buf.addfile(info, io.BytesIO(data))
        buf.close()

    def ext4(self, path):
        content = self.tmp / "rootfs"
        (content / "etc").mkdir(parents=True, exist_ok=True)
        (content / "etc/hostname").write_text("plinius\n")
        subprocess.run(["mke2fs", "-q", "-t", "ext4", "-d", str(content), str(path), "8M"], check=True)
        return path

    def serial(self, text):
        (self.qemu / "serial").write_text(text)

    def run(self, *args, **env):
        args = args or (self.image,)
        return subprocess.run(["bash", str(self.root / "image/test-image.sh"), *map(str, args)],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
                              env=self.fake_bin.env(isolated=True, FAKE_QEMU=self.qemu, **env))

    def out(self, name="plinius-arch-test"):
        return self.root / "build/image-test" / name

    def bsdtar_calls(self):
        log = self.tmp / "bsdtar.log"
        return log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def volla(tmp_path, fake_bin):
    return Volla(tmp_path, fake_bin)


def test_boot_test_of_an_image(volla):
    before = volla.image.read_bytes()
    r = volla.run()
    assert r.returncode == 0, r.stderr
    assert "boot test passed" in r.stderr
    out = volla.out()
    assert (out / "report.txt").read_text().startswith("Verdict: passed")
    assert "@@IMAGE-CHECK end" in (out / "serial.log").read_text()
    assert volla.image.read_bytes() == before
    # The kernel and its modules, unpacked once with the module database
    kdir = volla.root / "build/image-test/kernel/linux-aarch64-7.3-1-aarch64.pkg.tar.xz"
    assert (kdir / "version").read_text() == KVER + "\n"
    assert (kdir / "Image").read_bytes() == b"ARM64 kernel image"
    listing = subprocess.run(["debugfs", "-R", "ls -p /", str(kdir / "modules.img")],
                             capture_output=True, text=True).stdout
    assert "/modules.dep/" in listing and "/modules.builtin/" in listing
    assert not list(kdir.parent.glob("*.pkg.tar.xz.*"))  # no unpacking directory left


def test_how_qemu_starts_the_image(volla):
    assert volla.run().returncode == 0
    append = (volla.qemu / "append").read_text()
    assert append.startswith("root=/dev/vda rw console=ttyAMA0 systemd.journald.max_level_store=info ")
    assert "systemd.wants=image-check.service" in append
    assert f"systemd.mount-extra=LABEL=kmods:/usr/lib/modules/{KVER}:ext4:ro" in append
    cred = re.search(r"systemd\.set_credential_binary=systemd\.extra-unit\.image-check\.service:(\S+)",
                     append).group(1)
    unit = base64.b64decode(cred).decode()
    assert "After=multi-user.target graphical.target" in unit
    assert "ExecStart=/usr/bin/bash /dev/vdb" in unit
    drives = (volla.qemu / "drives").read_text().splitlines()
    out = volla.out()
    kdir = volla.root / "build/image-test/kernel/linux-aarch64-7.3-1-aarch64.pkg.tar.xz"
    assert drives == [f"file={volla.image},format=raw,if=virtio,snapshot=on",
                      f"file={out}/guest.img,format=raw,if=virtio,readonly=on",
                      f"file={kdir}/modules.img,format=raw,if=virtio,snapshot=on"]
    # QEMU keeps the snapshot next to the result, not in /tmp
    assert (volla.qemu / "tmpdir").read_text().strip() == str(out)


def test_the_check_carries_the_units_that_must_run(volla):
    assert volla.run().returncode == 0
    guest = (volla.out() / "guest.img").read_bytes()
    assert len(guest) % 512 == 0
    text = guest.rstrip(b"\0").decode()
    first, rest = text.split("\n", 1)
    rules = load_script(REPORT).read_rules(EXPECTED)
    assert first == 'ACTIVE="' + "".join(f"{u} " for u in rules["active"]) + '"'
    assert rest == (REPO / "image/test-image-guest.sh").read_text()


def test_the_kernel_is_unpacked_only_once(volla):
    assert volla.run().returncode == 0
    assert len(volla.bsdtar_calls()) == 1
    assert volla.run().returncode == 0
    assert len(volla.bsdtar_calls()) == 1


def test_the_newest_kernel_package_and_a_chosen_one(volla, tmp_path):
    volla.kernel_package("linux-aarch64-7.10-1-aarch64.pkg.tar.xz")
    (volla.root / "build/cache/pacman/linux-aarch64-9.0-1-aarch64.pkg.tar.xz.sig").write_text("sig")
    (volla.root / "build/cache/pacman/linux-aarch64-headers-9.0-1-aarch64.pkg.tar.xz").write_text("x")
    assert volla.run().returncode == 0
    assert (volla.root / "build/image-test/kernel/linux-aarch64-7.10-1-aarch64.pkg.tar.xz/Image").exists()
    chosen = volla.root / "build/cache/pacman/linux-aarch64-7.3-1-aarch64.pkg.tar.xz"
    r = volla.run(volla.image, tmp_path / "out", IMAGE_TEST_KERNEL=chosen)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "out/report.txt").exists()
    r = volla.run(IMAGE_TEST_KERNEL=tmp_path / "missing.pkg.tar.xz")
    assert r.returncode == 1 and "missing.pkg.tar.xz missing" in r.stderr


def test_a_kernel_package_without_a_kernel(volla):
    for f in (volla.root / "build/cache/pacman").iterdir():
        f.unlink()
    volla.kernel_package(image=False)
    r = volla.run()
    assert r.returncode == 1 and "has no boot/Image or modules" in r.stderr
    assert not list((volla.root / "build/image-test/kernel").iterdir())


def test_without_a_kernel_package(volla):
    for f in (volla.root / "build/cache/pacman").iterdir():
        f.unlink()
    r = volla.run()
    assert r.returncode == 1 and "no linux-aarch64 package" in r.stderr


def test_an_image_that_fails_its_boot(volla):
    volla.serial(volla.log("@@IMAGE-CHECK failed broken.service"))
    r = volla.run()
    assert r.returncode == 1
    assert "failed: broken.service" in r.stderr
    assert "plinius-arch-test.img failed the boot test, see" in r.stderr
    assert "Verdict: FAILED" in (volla.out() / "report.txt").read_text()


def test_a_boot_that_hangs_until_the_timeout(volla):
    volla.serial("[    0.0] booting\n")
    r = volla.run(FAKE_QEMU_EXIT="124", IMAGE_TEST_TIMEOUT="5")
    assert r.returncode == 1
    assert "the boot did not end within 5 s" in r.stderr
    assert "the check never started" in r.stderr


def test_what_is_not_an_image(volla, tmp_path):
    r = volla.run(tmp_path / "missing.img")
    assert r.returncode == 1 and "missing.img missing" in r.stderr
    packed = tmp_path / "packed.img"
    packed.write_bytes(b"\x28\xb5\x2f\xfd" + b"\0" * 4096)
    r = volla.run(packed)
    assert r.returncode == 1 and "is not an ext4 image" in r.stderr
    r = subprocess.run(["bash", str(volla.root / "image/test-image.sh")], capture_output=True,
                       text=True, env=volla.fake_bin.env(isolated=True))
    assert r.returncode != 0 and "usage: image/test-image.sh IMAGE.img" in r.stderr


def test_a_tool_missing_on_the_host(volla):
    (volla.fake_bin.path / "qemu-system-aarch64").unlink()
    r = volla.run()
    assert r.returncode == 1 and "qemu-system-aarch64 missing on the host" in r.stderr
