# plinius-swap-set (runs as root through pkexec without a PIN, so its argument checks matter) and
# the install script of plinius-swap. Only a copy with its paths in tmp_path is run; swapon,
# mkswap, fallocate and the block device tools are stubs.

import pytest

from conftest import PKG
from system_helpers import call_scriptlet, rewritten_copy, run, scriptlet

SRC = PKG / "plinius-swap" / "plinius-swap-set"
GIB = 1024 ** 3

SHIPPED_CONF = (PKG / "plinius-swap" / "plinius-swap.conf").read_text()


class Swap:
    def __init__(self, tmp_path, fake_bin):
        self.tmp = tmp_path
        self.bin = fake_bin
        self.conf = tmp_path / "etc" / "plinius-swap.conf"
        self.dir = tmp_path / "home" / ".plinius-swap"
        self.file = self.dir / "swapfile"
        self.swaps = tmp_path / "proc" / "swaps"
        self.meminfo = tmp_path / "proc" / "meminfo"
        self.swappiness = tmp_path / "proc" / "sys" / "vm" / "swappiness"
        for d in (self.conf.parent, tmp_path / "home", tmp_path / "run", self.swappiness.parent):
            d.mkdir(parents=True, exist_ok=True)
        self.conf.write_text(SHIPPED_CONF)
        self.swappiness.write_text("100\n")
        self.set_swaps([("/dev/block/zram0", "partition", 0)])
        self.set_meminfo(total=8_000_000, available=4_000_000)
        self.script = rewritten_copy(SRC, tmp_path / "plinius-swap-set", {
            "/etc/plinius-swap.conf": self.conf,
            "/home/.plinius-swap": self.dir,
            "/run/plinius-swap.lock": tmp_path / "run" / "plinius-swap.lock",
            "/proc/meminfo": self.meminfo,
            "/proc/swaps": self.swaps,
            "/proc/sys/vm/swappiness": self.swappiness,
        }, check_left=("/etc/", "/run/", "/proc/"))
        self.avail_kib = 100 * 1024 * 1024
        self.mounts = {"/": "/dev/sda20", "/home": "/dev/sda20"}
        self.stacks = {}  # device -> lsblk -s types
        self.loops = {}   # loop device -> backing file
        self.stubs()

    def set_swaps(self, areas):
        lines = ["Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority"]
        lines += [f"{n}\t{t}\t8388604\t{u}\t-2" for n, t, u in areas]
        self.swaps.write_text("\n".join(lines) + "\n")

    def set_meminfo(self, total, available):
        self.meminfo.write_text(f"MemTotal:       {total} kB\nMemFree:        1000 kB\n"
                                f"MemAvailable:   {available} kB\n")

    def stubs(self):
        b = self.bin
        b.stub("id", 'case $1 in -u) echo "${FAKE_UID:-0}" ;; *) echo root ;; esac')
        b.stub("mkswap")
        b.stub("sleep")
        b.stub("fallocate", 'truncate -s "$2" "$3"')
        b.stub("rmdir", '/usr/bin/rmdir "$@"')
        # /proc/swaps follows swapon/swapoff; the kernel finds an area by its device, so a name
        # under /dev/block and one under /dev match
        b.stub("swapon", f'prio=-2; [ "$1" = --priority ] && {{ prio=$2; shift 2; }}\n'
               f'case $1 in *zram*) t=partition ;; *) t=file ;; esac\n'
               f'printf "%s\\t%s\\t8388604\\t0\\t%s\\n" "$1" "$t" "$prio" >> "{self.swaps}"')
        b.stub("swapoff", f'awk -v n="${{1##*/}}" \'NR == 1 {{ print; next }} '
               f'{{ k = $1; sub(".*/", "", k); if (k != n) print }}\' "{self.swaps}" > '
               f'"{self.swaps}.new" && mv "{self.swaps}.new" "{self.swaps}"')

    def env(self, **extra):
        b = self.bin
        b.stub("findmnt", "t=${@: -1}\ncase $t in\n" + "".join(
            f'"{p}") echo "{s}" ;;\n' for p, s in self.mounts.items()) +
            '*) echo /dev/sda20 ;;\nesac')
        b.stub("lsblk", 'd=${@: -1}\ncase "$*" in\n' + "".join(
            f'*-snro*"{dev}") printf "%s\\n" {" ".join(types)} ;;\n'
            for dev, types in self.stacks.items()) +
            '*-snro*) printf "part\\ndisk\\n" ;;\n' + "".join(
            f'*-dno*"{dev}") echo loop ;;\n' for dev in self.loops) +
            '*-dno*) echo part ;;\nesac')
        b.stub("losetup", 'case ${@: -1} in\n' + "".join(
            f'"{dev}") echo "{back}" ;;\n' for dev, back in self.loops.items()) +
            '*) exit 1 ;;\nesac')
        b.stub("df", f'printf "Avail\\n%s\\n" {self.avail_kib}')
        return b.env(isolated=True, **extra)

    def run(self, *args, **extra):
        return run([self.script, *args], self.env(**extra))

    def conf_value(self, key):
        values = [line.split("=", 1)[1] for line in self.conf.read_text().splitlines()
                  if line.startswith(key + "=")]
        assert len(values) <= 1, f"{key} more than once in the configuration"
        return values[0] if values else None


@pytest.fixture
def swap(tmp_path, fake_bin):
    return Swap(tmp_path, fake_bin)


def test_without_arguments_usage(swap):
    r = swap.run()
    assert r.returncode == 2 and "usage" in r.stderr


def test_unknown_command_usage(swap):
    assert swap.run("format").returncode == 2


def test_needs_root(swap):
    r = swap.run("enable", FAKE_UID=1000)
    assert r.returncode == 1 and "root" in r.stderr
    assert swap.bin.calls("fallocate") == [] and swap.bin.calls("swapon") == []


@pytest.mark.parametrize("value", ["", "0", "129", "-1", "1.5", "8G", "abc", "8 9", "8;reboot",
                                   "../../etc", "$(id)", "09", "1" * 30])
def test_size_rejects_bad_values(swap, value):
    before = swap.conf.read_text()
    r = swap.run("size", value)
    assert r.returncode == 2
    assert swap.conf.read_text() == before
    assert swap.bin.calls("fallocate") == []


def test_size_missing_argument(swap):
    assert swap.run("size").returncode == 2


@pytest.mark.parametrize("value", ["", "0", "201", "-5", "abc", "1e3", "60 70", "60\n70"])
def test_swappiness_rejects_bad_values(swap, value):
    r = swap.run("swappiness", value)
    assert r.returncode == 2
    assert swap.swappiness.read_text() == "100\n"
    assert swap.conf_value("swappiness") == "1"


def test_swappiness_sets_kernel_and_file(swap):
    r = swap.run("swappiness", "60")
    assert r.returncode == 0, r.stderr
    assert swap.swappiness.read_text() == "60\n"
    assert swap.conf_value("swappiness") == "60"
    # The comments and the other keys of the shipped file stay
    text = swap.conf.read_text()
    assert "# vm.swappiness" in text
    assert swap.conf_value("enabled") == "yes" and swap.conf_value("size_gib") == "8"
    assert oct(swap.conf.stat().st_mode & 0o777) == "0o644"


@pytest.mark.parametrize("value", ["1", "200"])
def test_swappiness_limits_accepted(swap, value):
    assert swap.run("swappiness", value).returncode == 0
    assert swap.swappiness.read_text() == value + "\n"


def test_size_while_off_only_writes_the_setting(swap):
    r = swap.run("size", "4")
    assert r.returncode == 0, r.stderr
    assert swap.conf_value("size_gib") == "4"
    assert swap.bin.calls("fallocate") == [] and not swap.file.exists()


def test_enable_makes_the_file(swap):
    r = swap.run("enable")
    assert r.returncode == 0, r.stderr
    assert swap.file.stat().st_size == 8 * GIB
    assert oct(swap.dir.stat().st_mode & 0o777) == "0o700"
    assert oct(swap.file.stat().st_mode & 0o777) == "0o600"
    assert swap.bin.calls("fallocate") == [["-l", "8G", str(swap.file)]]
    assert swap.bin.calls("mkswap") == [[str(swap.file)]]
    # No priority of its own: zram (explicit priority) stays in front of the file
    assert swap.bin.calls("swapon") == [[str(swap.file)]]
    assert swap.conf_value("enabled") == "yes"


def test_enable_twice_keeps_the_file(swap):
    assert swap.run("enable").returncode == 0
    assert swap.run("enable").returncode == 0
    assert len(swap.bin.calls("fallocate")) == 1


def test_enable_refuses_home_on_loop(swap):
    swap.mounts["/home"] = "/dev/loop3"
    swap.loops["/dev/loop3"] = str(swap.tmp / "img")
    r = swap.run("enable")
    assert r.returncode == 5 and "loop" in r.stderr
    assert swap.bin.calls("fallocate") == [] and swap.bin.calls("swapon") == []


def test_enable_refuses_plain_home_on_encrypted_system(swap):
    swap.mounts["/"] = "/dev/dm-0"
    swap.stacks["/dev/dm-0"] = ["crypt", "part", "disk"]
    r = swap.run("enable")
    assert r.returncode == 5 and "encrypted" in r.stderr
    assert swap.bin.calls("fallocate") == []


def test_encryption_below_a_loop_root_is_found(swap):
    # Root is an image on a loop device whose backing file lies on the encrypted data partition:
    # the check has to follow the loop device, else root never looks encrypted
    image = swap.tmp / "userdata" / "rootfs.img"
    image.parent.mkdir()
    image.touch()
    swap.mounts.update({"/": "/dev/loop0", str(image): "/dev/dm-0", "/home": "/dev/sda21"})
    swap.loops["/dev/loop0"] = str(image)
    swap.stacks["/dev/dm-0"] = ["crypt", "part", "disk"]
    r = swap.run("enable")
    assert r.returncode == 5 and "encrypted" in r.stderr
    # With /home on the same encrypted partition the file is allowed
    swap.mounts["/home"] = "/dev/dm-0"
    r = swap.run("enable")
    assert r.returncode == 0, r.stderr
    assert swap.file.exists()


def test_enable_without_room_on_storage(swap):
    swap.avail_kib = 9 * 1024 * 1024  # 8 GiB plus less than the 2 GiB reserve
    r = swap.run("enable")
    assert r.returncode == 4
    assert not swap.file.exists() and swap.bin.calls("fallocate") == []


def test_failed_allocation_leaves_no_file(swap):
    swap.bin.stub("fallocate", 'truncate -s 1M "$3"; exit 1')
    r = swap.run("enable")
    assert r.returncode == 4
    assert not swap.file.exists()
    assert swap.conf_value("enabled") == "yes"  # unchanged shipped value, nothing new written


def test_broken_signature_is_made_again(swap):
    # The first swapon fails (say an interrupted mkswap), the second after a new file works
    marker = swap.tmp / "swapon-failed-once"
    swap.bin.stub("swapon", f'[ -e "{marker}" ] || {{ touch "{marker}"; exit 1; }}\n'
                  f'printf "%s\\tfile\\t1\\t0\\t-2\\n" "$1" >> "{swap.swaps}"')
    r = swap.run("enable")
    assert r.returncode == 0, r.stderr
    assert len(swap.bin.calls("fallocate")) == 2 and len(swap.bin.calls("swapon")) == 2


def test_disable_turns_off_and_deletes(swap):
    assert swap.run("enable").returncode == 0
    r = swap.run("disable")
    assert r.returncode == 0, r.stderr
    assert swap.bin.calls("swapoff") == [[str(swap.file)]]
    assert not swap.file.exists() and not swap.dir.exists()
    assert swap.conf_value("enabled") == "no"


def test_disable_refuses_when_ram_is_short(swap):
    assert swap.run("enable").returncode == 0
    # 3 GB in the file, 3.5 GB available of which a twentieth of 8 GB (400 MB) stays free
    swap.set_swaps([("/dev/block/zram0", "partition", 0), (str(swap.file), "file", 3_000_000)])
    swap.set_meminfo(total=8_000_000, available=3_300_000)
    r = swap.run("disable")
    assert r.returncode == 3
    assert swap.bin.calls("swapoff") == []
    assert swap.file.exists() and swap.conf_value("enabled") == "yes"


def test_size_change_remakes_an_active_file(swap):
    assert swap.run("enable").returncode == 0
    r = swap.run("size", "4")
    assert r.returncode == 0, r.stderr
    assert swap.file.stat().st_size == 4 * GIB
    assert swap.conf_value("size_gib") == "4"
    assert swap.bin.calls("fallocate")[-1] == ["-l", "4G", str(swap.file)]


def test_size_change_checks_room_before_touching_the_file(swap):
    assert swap.run("enable").returncode == 0
    swap.avail_kib = 1024 * 1024  # 1 GiB free + the old 8 GiB file < 16 GiB + reserve
    r = swap.run("size", "16")
    assert r.returncode == 4
    assert swap.bin.calls("swapoff") == []
    assert swap.file.stat().st_size == 8 * GIB and swap.conf_value("size_gib") == "8"


def test_drain_swaps_off_file_first_and_zram_back_in_front(swap):
    assert swap.run("enable").returncode == 0
    before = len(swap.bin.calls("swapon"))
    r = swap.run("drain")
    assert r.returncode == 0, r.stderr
    # The file before zram; zram by its /dev name (/dev/block only exists in the container)
    assert swap.bin.calls("swapoff") == [[str(swap.file)], ["/dev/zram0"]]
    assert swap.bin.calls("swapon")[before:] == [["--priority", "100", "/dev/zram0"],
                                                 [str(swap.file)]]


def test_drain_refuses_when_ram_is_short(swap):
    swap.set_swaps([("/dev/block/zram0", "partition", 3_900_000)])
    r = swap.run("drain")
    assert r.returncode == 3 and swap.bin.calls("swapoff") == []


def test_remove_restores_android_swappiness(swap):
    assert swap.run("swappiness", "30").returncode == 0
    assert swap.run("enable").returncode == 0
    r = swap.run("remove")
    assert r.returncode == 0, r.stderr
    assert not swap.file.exists()
    assert swap.swappiness.read_text() == "100\n"


def test_apply_waits_for_zram_without_real_sleeping(swap):
    swap.set_swaps([])
    r = swap.run("apply")
    assert r.returncode == 0, r.stderr
    assert "no zram" in r.stderr
    assert len(swap.bin.calls("sleep")) == 45  # 90 s in steps of 2 s
    assert swap.swappiness.read_text() == "1\n"
    assert swap.file.exists()


def test_apply_with_swap_switched_off_deletes_a_leftover_file(swap):
    swap.conf.write_text(SHIPPED_CONF.replace("enabled=yes", "enabled=no"))
    swap.dir.mkdir()
    swap.file.write_bytes(b"x")
    r = swap.run("apply")
    assert r.returncode == 0, r.stderr
    assert swap.bin.calls("sleep") == []
    assert not swap.file.exists() and swap.bin.calls("fallocate") == []


def test_apply_ignores_garbage_in_the_configuration(swap):
    # get() only takes [a-z0-9]: an injected value never reaches the kernel or a command line
    swap.conf.write_text("swappiness=$(touch /tmp/x)\nenabled=no\n")
    r = swap.run("apply")
    assert r.returncode == 0, r.stderr
    assert swap.swappiness.read_text() == "1\n"


def test_size_with_leading_zero_is_consistent(swap):
    r = swap.run("size", "010")
    if r.returncode == 2:
        return  # rejecting it is just as fine
    assert swap.run("enable").returncode == 0
    swap.set_swaps([("/dev/block/zram0", "partition", 0)])  # a reboot: the file is off
    assert swap.run("apply").returncode == 0
    assert len(swap.bin.calls("fallocate")) == 1, "the file was made again at the next boot"


# --- install script ---

def install_env(tmp_path, fake_bin, booted=True, chroot=False):
    fake_bin.stub("systemctl")
    fake_bin.stub("systemd-detect-virt", f"exit {0 if chroot else 1}")
    run_dir = tmp_path / "run-systemd"
    if booted:
        run_dir.mkdir(exist_ok=True)
    tool = fake_bin.stub("plinius-swap-set-stub")
    script = scriptlet("plinius-swap", tmp_path, {
        "/run/systemd/system": run_dir,
        "/usr/lib/plinius-swap/plinius-swap-set": tool,
    })
    return script, fake_bin.env(isolated=True)


def test_install_starts_service_on_running_system(tmp_path, fake_bin):
    script, env = install_env(tmp_path, fake_bin)
    assert call_scriptlet(script, "post_install", env).returncode == 0
    assert fake_bin.calls("systemctl") == [["daemon-reload"],
                                           ["start", "--no-block", "plinius-swap.service"]]


@pytest.mark.parametrize("booted,chroot", [(False, False), (True, True)])
def test_install_does_nothing_in_image_build(tmp_path, fake_bin, booted, chroot):
    script, env = install_env(tmp_path, fake_bin, booted=booted, chroot=chroot)
    assert call_scriptlet(script, "post_install", env).returncode == 0
    assert call_scriptlet(script, "pre_remove", env).returncode == 0
    assert fake_bin.calls("systemctl") == [] and fake_bin.calls("plinius-swap-set-stub") == []


def test_pre_remove_runs_remove_and_never_fails(tmp_path, fake_bin):
    script, env = install_env(tmp_path, fake_bin)
    fake_bin.stub("plinius-swap-set-stub", "exit 1")
    assert call_scriptlet(script, "pre_remove", env).returncode == 0
    assert fake_bin.calls("plinius-swap-set-stub") == [["remove"]]
