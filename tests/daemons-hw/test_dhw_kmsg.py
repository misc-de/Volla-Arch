# plinius-kmsg: the kernel log into the journal without the vendor drivers' status lines.
# The harness feeds scripted /dev/kmsg records (one read() each, as the kernel hands them out),
# errors of read() and a boot clock; journal_sendv() prints the fields it was given. Checked:
# the fields journald would have stored, what the drop lists leave out and what they never may,
# the hourly count, the sequence number across a restart, and the real drop.list against lines
# measured on the phone.

import re
import textwrap

import pytest

from conftest import REPO
from dhw_harness import PKG, build_harness, run, sandboxed_source

SRC = PKG / "plinius-kmsg" / "plinius-kmsg.c"
DROP = PKG / "plinius-kmsg" / "drop.list"

HARNESS = r"""
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

static FILE *script;
static long fake_clock;

/* One line of the script per read(): a record ("\n" in it is a newline), or an error */
static ssize_t
fake_read (int fd, void *buf, size_t size)
{
	static char line[16384];

	(void) fd;
	while (fgets (line, sizeof line, script)) {
		char *out = buf, *s;
		size_t n = 0;

		line[strcspn (line, "\n")] = '\0';
		if (sscanf (line, "clock %ld", &fake_clock) == 1)
			continue;
		if (!strcmp (line, "TERM")) {
			/* systemd stops the service while it reads: the handler, then EINTR */
			raise (SIGTERM);
			errno = EINTR;
			return -1;
		}
		if (!strcmp (line, "EAGAIN") || !strcmp (line, "EPIPE") || !strcmp (line, "EINTR") ||
		    !strcmp (line, "EIO")) {
			errno = !strcmp (line, "EAGAIN") ? EAGAIN : !strcmp (line, "EPIPE") ? EPIPE :
				!strcmp (line, "EINTR") ? EINTR : EIO;
			return -1;
		}
		for (s = line; *s && n < size; s++) {
			if (s[0] == '\\' && s[1] == 'n') {
				out[n++] = '\n';
				s++;
			} else {
				out[n++] = *s;
			}
		}
		return n;
	}
	return 0;
}

static int poll_fails;

static int
fake_poll (struct pollfd *fds, nfds_t n, int timeout)
{
	(void) fds;
	(void) n;
	printf ("poll %d\n", timeout);
	if (poll_fails) {
		errno = EBADF;
		return -1;
	}
	return 1;
}

static int
fake_nanosleep (const struct timespec *t, struct timespec *rest)
{
	(void) rest;
	printf ("coalesce %ld\n", (long) t->tv_sec * 1000 + t->tv_nsec / 1000000);
	return 0;
}

static int
fake_clock_gettime (clockid_t id, struct timespec *ts)
{
	(void) id;
	ts->tv_sec = fake_clock;
	ts->tv_nsec = 0;
	return 0;
}

static int
fake_sendv (const struct iovec *iov, int n)
{
	printf ("journal");
	for (int k = 0; k < n; k++)
		printf (" | %.*s", (int) iov[k].iov_len, (const char *) iov[k].iov_base);
	printf ("\n");
	return 0;
}

#define kmsg_read fake_read
#define kmsg_poll fake_poll
#define journal_sendv fake_sendv
#define clock_gettime fake_clock_gettime
#define coalesce_sleep fake_nanosleep
#define main real_main
#include "plinius-kmsg.c"
#undef main

int
main (int argc, char **argv)
{
	script = fopen (argv[1], "r");
	poll_fails = argc > 2;
	return real_main ();
}
"""


@pytest.fixture
def kmsg(tmp_path):
    """run(records, drop=..., local=...) → (journal lines, other output, exit code)"""
    state = tmp_path / "run"
    state.mkdir()
    dev = tmp_path / "dev-kmsg"
    dev.write_text("")
    src = sandboxed_source(SRC, tmp_path, [
        ('"/dev/kmsg"', f'"{dev}"'),
        ('"/usr/lib/plinius-kmsg/drop.list"', f'"{tmp_path}/drop.list"'),
        ('"/etc/plinius-kmsg/drop.list"', f'"{tmp_path}/local.list"'),
        ('"/run/plinius-kmsg/seqnum"', f'"{state}/seqnum"'),
    ])
    # The harness includes the sandboxed copy, which lies next to it
    assert src.parent == tmp_path
    exe = build_harness(tmp_path, "kmsg", HARNESS, pkgs=("libsystemd",))

    def go(*records, drop="", local=None, poll_fails=False, check=True):
        (tmp_path / "drop.list").write_text(textwrap.dedent(drop))
        if local is not None:
            (tmp_path / "local.list").write_text(textwrap.dedent(local))
        script = tmp_path / "script"
        script.write_text("".join(f"{r}\n" for r in records))
        r = run(exe, script, *(["fail"] if poll_fails else []), check=False)
        if check:
            assert r.returncode == 0, r.stdout + r.stderr
        lines = r.stdout.splitlines()
        return ([line for line in lines if line.startswith("journal")],
                [line for line in lines if not line.startswith("journal")], r)

    go.state = state / "seqnum"
    go.dev = dev
    return go


def fields(line):
    return line.split(" | ")[1:]


# ---- What goes to the journal ----

def test_a_kernel_line_keeps_what_journald_would_keep(kmsg):
    journal, _, _ = kmsg("3,812,22094317,-,caller=T1;usb 1-1: new device\\n SUBSYSTEM=usb\\n"
                         " DEVICE=c189:1\\n OTHER=x")
    assert fields(journal[0]) == [
        "MESSAGE=usb 1-1: new device", "PRIORITY=3", "SYSLOG_FACILITY=0",
        "SYSLOG_IDENTIFIER=kernel", "KERNEL_USEC=22094317", "KERNEL_SEQNUM=812",
        "KERNEL_SUBSYSTEM=usb", "KERNEL_DEVICE=c189:1"]


@pytest.mark.parametrize("text,ident,pid,message", [
    ("NetlinkListener[3869]: link up", "NetlinkListener", "3869", "link up"),
    ("healthd: battery l=74", "healthd", None, "battery l=74"),
    # Not a name the way journald reads one: the whole line stays the message
    ("no colon here", "kernel", None, "no colon here"),
    (": starts with a colon", "kernel", None, ": starts with a colon"),
    ("name[12 no bracket end", "kernel", None, "name[12 no bracket end"),
    ("name[12]x: text", "kernel", None, "name[12]x: text"),
])
def test_lines_from_user_space_carry_their_writer(kmsg, text, ident, pid, message):
    journal, _, _ = kmsg(f"12,5,100,-;{text}")
    f = fields(journal[0])
    assert f[0] == f"MESSAGE={message}"
    assert "SYSLOG_FACILITY=1" in f and "PRIORITY=4" in f
    assert f"SYSLOG_IDENTIFIER={ident}" in f
    assert (f"SYSLOG_PID={pid}" in f) == (pid is not None)


def test_escaped_bytes_come_back(kmsg):
    journal, _, _ = kmsg("6,1,1,-;tab\\x09end, a broken \\x4")
    assert fields(journal[0])[0] == "MESSAGE=tab\tend, a broken \\x4"


def test_records_that_are_not_ones_are_left(kmsg):
    journal, _, _ = kmsg("garbage", "6,2,3,- without semicolon", "6,3,4,-;fine")
    assert [fields(j)[0] for j in journal] == ["MESSAGE=fine"]


# ---- Dropping ----

DROP_LIST = """\
    # comment
    [cmdq] cmdq_dump_usage:   \t

    layer_info is null
    """


def test_listed_strings_are_left_out_and_counted(kmsg):
    journal, _, _ = kmsg("5,1,1,-;[cmdq] cmdq_dump_usage: hwid:0",
                         "3,2,2,-;[DISP][E]mtk_release_layer_fence:518 layer_info is null",
                         "5,3,3,-;cmdq_dump_usage without the prefix", drop=DROP_LIST)
    assert [fields(j)[0] for j in journal] == ["MESSAGE=cmdq_dump_usage without the prefix"]


def test_crit_alert_and_emerg_are_never_dropped(kmsg):
    journal, _, _ = kmsg(*(f"{p},{p + 1},1,-;layer_info is null" for p in range(4)),
                         drop=DROP_LIST)
    assert [fields(j)[1] for j in journal] == ["PRIORITY=0", "PRIORITY=1", "PRIORITY=2"]


def test_the_owners_list_comes_on_top(kmsg):
    journal, _, r = kmsg("6,1,1,-;my noisy driver", "6,2,2,-;layer_info is null", "6,3,3,-;kept",
                         drop=DROP_LIST, local="my noisy\n" + "x" * 256 + "\n")
    assert [fields(j)[0] for j in journal] == ["MESSAGE=kept"]
    assert "ignored (too long or too many)" in r.stderr


def test_no_lists_drop_nothing(kmsg, tmp_path):
    (tmp_path / "drop.list").unlink(missing_ok=True)
    journal, _, _ = kmsg("6,1,1,-;layer_info is null")
    assert len(journal) == 1


def test_at_most_256_strings(kmsg):
    journal, _, r = kmsg("6,1,1,-;<pattern 255>", "6,2,2,-;<pattern 256>",
                         drop="".join(f"<pattern {k}>\n" for k in range(257)))
    assert [fields(j)[0] for j in journal] == ["MESSAGE=<pattern 256>"]
    assert "<pattern 256>" in r.stderr


def test_one_notice_an_hour_with_the_first_line_after_it(kmsg):
    journal, _, _ = kmsg(
        "clock 100", "5,1,1,-;layer_info is null", "5,2,2,-;[cmdq] cmdq_dump_usage: a",
        "5,3,3,-;[cmdq] cmdq_dump_usage: b",
        "clock 3699", "6,4,4,-;still the first hour",
        "clock 3700", "6,5,5,-;the next hour",
        # An hour without anything dropped says nothing
        "clock 7300", "6,6,6,-;quiet hour over", drop=DROP_LIST)
    assert [fields(j)[0] for j in journal] == [
        "MESSAGE=still the first hour",
        "MESSAGE=3 vendor status lines left out of the journal in the last hour, 2 of them with "
        "\"[cmdq] cmdq_dump_usage:\" (all of them in dmesg)",
        "MESSAGE=the next hour", "MESSAGE=quiet hour over"]
    assert fields(journal[1])[1:] == ["PRIORITY=5", "SYSLOG_IDENTIFIER=plinius-kmsg"]


# ---- Reading /dev/kmsg ----

def test_the_last_line_survives_a_restart(kmsg):
    _, other, _ = kmsg("6,7,1,-;one", "6,8,2,-;two", "EAGAIN")
    # After the wake-up a pause, so a burst is read in one go
    assert other == ["poll -1", "coalesce 2000"]
    # Always the same length: one write over the old one is never half of two numbers
    assert kmsg.state.read_text() == "00000000000000000008\n"
    # The same buffer again after a restart: only what is new
    journal, _, _ = kmsg("6,7,1,-;one", "6,8,2,-;two", "6,9,3,-;three")
    assert [fields(j)[0] for j in journal] == ["MESSAGE=three"]
    assert int(kmsg.state.read_text()) == 9


def test_a_stop_saves_the_last_line_and_ends(kmsg):
    journal, other, r = kmsg("6,1,1,-;one", "TERM", "6,2,2,-;never")
    assert [fields(j)[0] for j in journal] == ["MESSAGE=one"]
    assert other == [] and r.returncode == 0
    assert int(kmsg.state.read_text()) == 1


def test_a_stop_during_the_wait_ends_after_saving(kmsg):
    _, other, r = kmsg("6,1,1,-;one", "EINTR", "EAGAIN", "TERM", "EAGAIN", "6,2,2,-;never")
    assert other == ["poll -1", "coalesce 2000"] and r.returncode == 0


def test_a_burst_without_new_lines_writes_nothing(kmsg):
    kmsg.state.write_text("8\n")
    kmsg("6,8,2,-;old", "EAGAIN")
    kmsg.state.unlink()
    kmsg("EAGAIN", "EINTR")
    assert not kmsg.state.exists()


def test_a_state_that_cannot_be_written_stays_dirty(kmsg):
    kmsg.state.parent.chmod(0o500)
    try:
        _, _, r = kmsg("6,1,1,-;one", "EAGAIN")
    finally:
        kmsg.state.parent.chmod(0o700)
    assert not kmsg.state.exists()


def test_unreadable_state_is_no_state(kmsg):
    kmsg.state.write_text("not a number\n")
    journal, _, _ = kmsg("6,1,1,-;one")
    assert len(journal) == 1


def test_an_overrun_buffer_is_said(kmsg):
    journal, _, _ = kmsg("EPIPE", "6,90,1,-;after")
    assert fields(journal[0]) == [
        "MESSAGE=kernel log: lines lost, the kernel's buffer ran over before they were read",
        "PRIORITY=5", "SYSLOG_IDENTIFIER=plinius-kmsg"]
    assert fields(journal[1])[0] == "MESSAGE=after"


def test_a_read_error_ends_the_service(kmsg):
    journal, _, r = kmsg("6,1,1,-;one", "EIO", "6,2,2,-;never", check=False)
    assert r.returncode == 1 and "Input/output error" in r.stderr
    assert len(journal) == 1
    assert int(kmsg.state.read_text()) == 1


def test_a_failing_poll_ends_the_loop(kmsg):
    _, other, r = kmsg("EAGAIN", "6,1,1,-;never", poll_fails=True)
    assert other == ["poll -1"] and r.returncode == 0


def test_no_kernel_log_no_service(kmsg):
    kmsg.dev.unlink()
    _, _, r = kmsg(check=False)
    assert r.returncode == 1 and "No such file or directory" in r.stderr


# ---- The list shipped with the package, against lines measured on the phone ----

NOISE = [
    "11,1,1,-;NetlinkListener: recvmsg failed (I/O error)",
    "12,2,1,-;selinux: SELinux: SELinux is disabled, skipping restorecon",
    "3,3,1,-;power_supply upm6722-standalone: driver failed to report `online' property: -22",
    "5,4,1,-;[cmdq] cmdq_dump_usage: hwid:0 suspend:0 usage:0 fast_usage:0",
    "5,5,1,-;mtk_mminfra_pd_callback: disable gce apsrc: 0x1e980048=0x0",
    "3,6,1,-;[DPTX][ERROR]mtk_dp_get_dsc_capability: dp not initial",
    "3,7,1,-;[DISP][E]mtk_release_layer_fence:518 layer_info is null",
    "3,8,1,-;[sh366100] fg_refresh_status(2045): RSOC:73, Volt:4056000, Current:384000",
    "3,9,1,-;[cs_chrg] temp_c:19 zones[3].temp_c:55 ",
    "4,10,1,-;lpp----CURRENT_AVG val->intval=-452000",
    "12,11,1,-;healthd: battery l=74 v=4078 t=20.0 h=2 st=2 c=401000 fc=4918000 cc=18 chg=u",
    "3,12,1,-;mtk-msdc 11240000.mmc: At time:1841444388 dat_crc happen cmd=19; blocks=1; xfer_size=0;",
    "3,13,1,-;mtk-msdc 11240000.mmc: At time:1841364619 cmd_crc happen cmd=19 arg=0x0; rsp 0x900;",
    "5,14,1,-;GDL-0[W:2] [gps_dl_hist_rec_rw_do_dump:50]: rd: dp=0, pid=7932, i=0, n=8(1)",
]
KEPT = [
    '5,20,1,-;audit: type=1400 audit(1795596990.716:442): apparmor="DENIED" operation="open" '
    'profile="plinius.ofonod"',
    "3,21,1,-;mtk-msdc 11240000.mmc: At time:1 dat_crc happen cmd=18; blocks=8; xfer_size=4096;",
    "4,22,1,-;x29: ffffffc008ceb600 x28: ffffffc008f5c3c0 x27: ffffff800d942638",
    "6,23,1,-;PM: suspend entry (deep)",
    "3,24,1,-;Trustonic TEE: ERROR -1 mcp_cmd: open session: res 8",
    "6,25,1,-;[FM] connsys adie=0x6637 ok",
    "0,26,1,-;mt5706_set_gpio_state: irq_gpio[1, 0, 0x0] set 0x0 --> 0x10",
    "3,27,1,-;Out of memory: Killed process 1234 (firefox)",
]


def test_the_shipped_list_drops_the_measured_noise_and_keeps_the_rest(kmsg):
    journal, _, _ = kmsg(*NOISE, *KEPT, drop=DROP.read_text())
    seqs = [re.search(r"KERNEL_SEQNUM=(\d+)", j).group(1) for j in journal]
    assert seqs == [str(n) for n in range(20, 28)]


def test_the_shipped_list_is_well_formed():
    patterns = [line for line in DROP.read_text().splitlines()
                if line.strip() and not line.startswith("#")]
    assert len(patterns) == len(set(patterns)) <= 256
    assert all(p == p.strip() and len(p) <= 255 for p in patterns)
    # Never a string so short that it would hit unrelated lines
    assert min(map(len, patterns)) >= 8


# ---- Wiring ----

def test_journald_leaves_the_kernel_log_to_the_service():
    conf = (PKG / "device-volla-ansuz" / "50-plinius-journald.conf").read_text()
    assert re.search(r"^ReadKMsg=no$", conf, re.M)
    device = (PKG / "device-volla-ansuz" / "PKGBUILD").read_text()
    assert re.search(r"^depends=\(.*\bplinius-kmsg\b", device, re.M)
    unit = (PKG / "plinius-kmsg" / "plinius-kmsg.service").read_text()
    for line in ("DefaultDependencies=no", "After=systemd-journald.socket", "Before=sysinit.target",
                 "LogRateLimitIntervalSec=0", "CapabilityBoundingSet=CAP_SYSLOG",
                 "DeviceAllow=/dev/kmsg r", "RuntimeDirectoryPreserve=yes"):
        assert re.search(rf"^{re.escape(line)}$", unit, re.M), line
    # ProtectKernelLogs= would take /dev/kmsg away
    assert "ProtectKernelLogs" not in unit
    pkgbuild = (PKG / "plinius-kmsg" / "PKGBUILD").read_text()
    assert "sysinit.target.wants" in pkgbuild


def test_readers_of_the_kernel_log_find_it_without_the_kernel_transport():
    # `journalctl -k` sees only what journald read itself; the service's lines are -t kernel
    for path in (REPO / "tools" / "aa-denials.sh",
                 PKG / "device-volla-ansuz" / "plinius-hardware-info"):
        text = path.read_text()
        assert not re.search(r"journalctl[^|;\n]*\s-k\b", text), path
        assert "journalctl -t kernel" in text or "journalctl -b -t kernel" in text, path
