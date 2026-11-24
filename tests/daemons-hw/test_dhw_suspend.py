# plinius-suspend: one suspend attempt (wakeup_count handshake, never with the display on),
# the retry back-off, the USB host and display checks, what keeps the phone awake in the log. /sys/power and the DRM connector are
# files in tmp, so "mem" is written to a plain file; the D-Bus parts (logind inhibitors,
# timesyncd, the mce signal) are not run.

import functools
import os
import shutil

import pytest

from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "plinius-suspend" / "plinius-suspend.c"

HARNESS = r"""
#include <glib.h>
#include <gio/gio.h>
#include <stdio.h>
#include <stdlib.h>
#define main real_main
#include "plinius-suspend.c"
#undef main

static const char *names[] = {"slept", "refused", "cancelled", "failed"};

static AttemptResult *attempt(void)
{
	GTask *task = g_task_new(NULL, NULL, NULL, NULL);
	attempt_thread(task, NULL, NULL, NULL);
	AttemptResult *res = g_task_propagate_pointer(task, NULL);
	g_object_unref(task);
	return res;
}

/* Feed a result to on_attempt_done as the worker would */
static void done(Attempt result, int error)
{
	GTask *task = g_task_new(NULL, NULL, NULL, NULL);
	AttemptResult *res = g_new0(AttemptResult, 1);
	res->result = result;
	res->error = error;
	res->slept = result == ATTEMPT_SLEPT ? 30 : 0;
	g_task_return_pointer(task, res, g_free);
	st.busy = TRUE;
	on_attempt_done(NULL, G_ASYNC_RESULT(task), NULL);
	g_object_unref(task);
}

int main(int argc, char **argv)
{
	st.off_delay_ms = 5000;
	st.awake_delay_ms = 2000;
	for (int i = 1; i < argc; i++) {
		const char *a = argv[i];
		if (!strcmp(a, "attempt")) {
			AttemptResult *res = attempt();
			printf("attempt=%s %d\n", names[res->result], res->error);
			g_free(res);
		} else if (!strcmp(a, "cancel")) {
			g_atomic_int_set(&st.cancel, 1);
		} else if (!strcmp(a, "display-off")) {
			set_display(TRUE);
			printf("display: off=%d timer=%d retry=%u\n", st.display_off, st.timer != 0, st.retry_ms);
		} else if (!strcmp(a, "display-on")) {
			set_display(FALSE);
			printf("display: off=%d timer=%d cancel=%d\n", st.display_off, st.timer != 0, st.cancel);
		} else if (g_str_has_prefix(a, "done:")) {
			Attempt r = !strcmp(a + 5, "slept") ? ATTEMPT_SLEPT : !strcmp(a + 5, "refused") ? ATTEMPT_REFUSED
				  : !strcmp(a + 5, "failed") ? ATTEMPT_FAILED : ATTEMPT_CANCELLED;
			done(r, EIO);
			printf("%s: retry=%u suspends=%u slept=%.0f timer=%d busy=%d\n", a, st.retry_ms, st.suspends,
			       st.slept, st.timer != 0, st.busy);
		} else if (!strcmp(a, "usb")) {
			printf("usb=%d\n", usb_host_connected());
		} else if (!strcmp(a, "off?")) {
			printf("off?=%d\n", display_is_off());
		} else if (!strcmp(a, "sources")) {
			char *sources = active_wakeup_sources();
			printf("sources=%s\n", sources);
			g_free(sources);
		} else if (!strcmp(a, "timer")) {
			/* Only with a USB host or the display on: the logind check needs the bus */
			on_timer(NULL);
			printf("timer: busy=%d timer=%d\n", st.busy, st.timer != 0);
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("suspend")
    sandboxed_source(SRC, d, [
        ("/sys/class/drm/card0-DSI-1/dpms", d / "dpms"),
        ("/sys/class/udc", d / "udc"),
        ("/sys/power/wakeup_count", d / "wakeup_count"),
        ("/sys/power/state", d / "state"),
        ("/sys/kernel/wakeup_reasons/last_resume_reason", d / "resume_reason"),
        ("/sys/class/wakeup", d / "wakeup"),
        ("/run/user/*/plinius-aod", f"{d}/run-user/*/plinius-aod"),
    ])
    exe = build_harness(d, "suspend", HARNESS, pkgs=("gio-unix-2.0",))
    return d, functools.partial(run, exe)


@pytest.fixture
def susp(built):
    d, binary = built
    for name in ("dpms", "wakeup_count", "state", "resume_reason"):
        p = d / name
        if p.is_dir():
            p.rmdir()
        p.unlink(missing_ok=True)
    shutil.rmtree(d / "udc", ignore_errors=True)
    shutil.rmtree(d / "wakeup", ignore_errors=True)
    (d / "udc/musb-hdrc").mkdir(parents=True)
    (d / "udc/musb-hdrc/state").write_text("not attached\n")
    (d / "dpms").write_text("Off\n")
    (d / "wakeup_count").write_text("1234\n")
    os.utime(d / "wakeup_count", ns=(0, 0))
    (d / "state").write_text("")
    (d / "resume_reason").write_text("Resume caused by IRQ 123\n")
    return d, binary


def test_attempt_writes_count_then_mem(susp):
    d, binary = susp
    assert kv(binary("attempt").stdout)["attempt"] == "slept 0"
    # The wakeup_count handshake: the count is written back before "mem"
    assert (d / "wakeup_count").stat().st_mtime_ns != 0
    assert (d / "wakeup_count").read_text().startswith("1234")
    assert (d / "state").read_text() == "mem"


def test_never_suspends_with_the_display_on(susp):
    d, binary = susp
    (d / "dpms").write_text("On\n")
    assert kv(binary("attempt").stdout)["attempt"] == "cancelled 0"
    assert (d / "state").read_text() == ""


@pytest.mark.parametrize("dpms,off", [("Off\n", 1), ("On\n", 0), ("Standby\n", 0), ("", 0)])
def test_display_is_off(susp, dpms, off):
    d, binary = susp
    (d / "dpms").write_text(dpms)
    assert kv(binary("off?").stdout)["off?"] == str(off)


def test_missing_connector_counts_as_on(susp):
    d, binary = susp
    (d / "dpms").unlink()
    assert kv(binary("attempt").stdout)["attempt"] == "cancelled 0"
    assert (d / "state").read_text() == ""


def test_cancelled_when_the_display_came_on(susp):
    d, binary = susp
    assert kv(binary("cancel", "attempt").stdout)["attempt"] == "cancelled 0"
    assert (d / "state").read_text() == ""


def test_unreadable_wakeup_count_is_refused(susp):
    d, binary = susp
    (d / "wakeup_count").unlink()
    assert kv(binary("attempt").stdout)["attempt"] == "refused 0"
    assert (d / "state").read_text() == ""


def test_failed_suspend(susp):
    d, binary = susp
    (d / "state").unlink()
    (d / "state").mkdir()          # cannot be opened for writing
    out = kv(binary("attempt").stdout)
    assert out["attempt"] == f"failed {21}"   # EISDIR


def test_retry_back_off(susp):
    d, binary = susp
    out = binary("display-off", "done:refused", "done:refused", "done:refused", "done:refused",
                 "done:refused", "done:slept", "done:refused", "done:failed", "done:cancelled",
                 "display-on", "done:cancelled").stdout
    lines = [line for line in out.splitlines() if line.startswith("done:")]
    retries = [line.split("retry=")[1].split()[0] for line in lines]
    # Doubles up to 10 s; a successful sleep starts again at 1 s
    assert retries == ["2000", "4000", "8000", "10000", "10000", "1000", "2000", "2000", "2000", "2000"]
    assert "suspends=1 slept=30" in lines[5]
    assert all("busy=0" in line for line in lines)
    # With the display on nothing is scheduled any more
    assert lines[-1].endswith("timer=0 busy=0")
    assert "display: off=0 timer=0 cancel=1" in out


def test_usb_host(susp):
    d, binary = susp
    assert kv(binary("usb").stdout)["usb"] == "0"
    (d / "udc/musb-hdrc/state").write_text("configured\n")
    assert kv(binary("usb").stdout)["usb"] == "1"


def test_timer_waits_for_usb_host_and_display(susp):
    d, binary = susp
    (d / "udc/musb-hdrc/state").write_text("configured\n")
    out = binary("display-off", "timer").stdout
    # USB host: no attempt, look again later
    assert "timer: busy=0 timer=1" in out
    (d / "udc/musb-hdrc/state").write_text("not attached\n")
    (d / "dpms").write_text("On\n")
    out = binary("display-off", "timer").stdout
    assert "timer: busy=0 timer=1" in out
    assert (d / "state").read_text() == ""


def wakeup_source(d, entry, name, active_ms):
    w = d / "wakeup" / entry
    w.mkdir(parents=True)
    if name is not None:
        (w / "name").write_text(name + "\n")
    (w / "active_time_ms").write_text(f"{active_ms}\n")


def test_active_wakeup_sources(susp):
    d, binary = susp
    # No class directory (an old kernel): nothing to name
    assert kv(binary("sources").stdout)["sources"] == "none"
    wakeup_source(d, "wakeup3", "bt_drv_io", 812400)
    wakeup_source(d, "wakeup4", "WLAN timeout", 0)
    wakeup_source(d, "wakeup5", None, 1500)
    (d / "wakeup/wakeup6").mkdir()       # active_time_ms unreadable: skipped
    sources = kv(binary("sources").stdout)["sources"]
    # Only the active ones, with their active time; without a name file the entry's name
    assert sorted(sources.split(", ")) == ["bt_drv_io (812 s)", "wakeup5 (2 s)"]


def test_refusals_in_a_row_name_the_wakeup_sources(susp):
    d, binary = susp
    wakeup_source(d, "wakeup0", "bt_drv_io", 60000)
    r = binary("display-off", *["done:refused"] * 7, "done:slept", *["done:refused"] * 6)
    # Once at the sixth refusal in a row, again only after a sleep ended the series
    assert r.stderr.count("suspend refused 6 times in a row, active wakeup sources: bt_drv_io (60 s)") == 2
    assert "7 times" not in r.stderr


# ---- The service as it runs: main(), the mce signal, logind and timesyncd over a scripted bus ----
#
# fakes/dhw_scripted_gdbus.h answers the D-Bus calls; the main loop runs the script given after
# "--": reply:<key>=<GVariant text> scripts an answer, display:off|on delivers plinius-mce's
# signal, run:<ms> lets the main loop run (timers, the worker thread), state prints the state.
# The kernel's answers that a plain file cannot give (EBUSY on "mem", the alarm timer that needs
# CAP_WAKE_ALARM) come from wrappers around open/write/timerfd_*.

SERVICE = r"""
#define G_DISABLE_CAST_CHECKS
#include <errno.h>
#include <fcntl.h>
#include <glib.h>
#include <gio/gio.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/timerfd.h>
#include <unistd.h>
#include "dhw_scripted_gdbus.h"

static void script_loop(GMainLoop *loop);
#define g_main_loop_run script_loop

/* The alarm timer: 0 refused as without CAP_WAKE_ALARM, 1 armed (as a CLOCK_REALTIME timer), 2
 * timerfd_settime fails */
static int alarm_mode;
static int alarm_clock = -1;
static struct itimerspec alarm_when;
static int alarm_fd = -1;
static int fake_timerfd_create(int clock, int flags)
{
	alarm_clock = clock;
	if (alarm_mode == 0) {
		errno = EPERM;
		return -1;
	}
	return alarm_fd = timerfd_create(CLOCK_REALTIME, flags);
}
static int fake_timerfd_settime(int fd, int flags, const struct itimerspec *when, struct itimerspec *old)
{
	alarm_when = *when;
	if (alarm_mode == 2) {
		errno = EINVAL;
		return -1;
	}
	return timerfd_settime(fd, flags, when, old);
}
#define timerfd_create fake_timerfd_create
#define timerfd_settime fake_timerfd_settime

/* The kernel's answers: "mem" refused with this errno; the display coming on during the
 * handshake (after wakeup_count is written back) */
static int mem_errno;
static int on_during_handshake;
static int mem_fd = -1, count_fd = -1;
static gint *cancel_flag;          /* &st.cancel, set once st is declared */
static int fake_open(const char *path, int flags)
{
	int fd = open(path, flags);
	if (g_str_has_suffix(path, "/state"))
		mem_fd = fd;
	if (g_str_has_suffix(path, "/wakeup_count"))
		count_fd = fd;
	return fd;
}
static ssize_t fake_write(int fd, const void *buf, size_t n)
{
	if (fd == mem_fd && mem_errno) {
		errno = mem_errno;
		return -1;
	}
	ssize_t r = write(fd, buf, n);
	if (fd == count_fd && on_during_handshake)
		g_atomic_int_set(cancel_flag, 1);
	return r;
}
static int fake_close(int fd)
{
	if (fd == mem_fd)
		mem_fd = -1;
	if (fd == count_fd)
		count_fd = -1;
	if (fd == alarm_fd) {
		printf("alarm closed\n");
		alarm_fd = -1;
	}
	return close(fd);
}
/* Attempts started in the worker thread */
static int attempts;
static void counted_run_in_thread(GTask *task, GTaskThreadFunc fn)
{
	attempts++;
	g_task_run_in_thread(task, fn);
}
#define open fake_open
#define write fake_write
#define close fake_close
#define g_task_run_in_thread counted_run_in_thread
/* Fatal as in the daemon, but through exit(): gcov keeps the counters of the run */
#define g_error(...) (g_printerr(__VA_ARGS__), g_printerr("\n"), exit(99))

#define main real_main
#include "plinius-suspend.c"
#undef main
#undef g_main_loop_run
#undef open
#undef write
#undef close
#undef g_task_run_in_thread

static char **script;

static gboolean quit_loop(gpointer loop)
{
	g_main_loop_quit(loop);
	return G_SOURCE_REMOVE;
}

static void run_for(unsigned ms)
{
	GMainLoop *loop = g_main_loop_new(NULL, FALSE);
	g_timeout_add(ms, quit_loop, loop);
	g_main_loop_run(loop);
	g_main_loop_unref(loop);
}

static void print_state(void)
{
	printf("state off=%d busy=%d timer=%d suspends=%u retry=%u timesync_stopped=%d off_delay=%u awake_delay=%u\n",
	       st.display_off, st.busy, st.timer != 0, st.suspends, st.retry_ms, st.timesync_stopped,
	       st.off_delay_ms, st.awake_delay_ms);
	fflush(stdout);
}

static void command(const char *a)
{
	if (g_str_has_prefix(a, "reply:")) {
		g_auto(GStrv) kv = g_strsplit(a + 6, "=", 2);
		sg_reply(kv[0], kv[1]);
	} else if (g_str_has_prefix(a, "display:")) {
		g_autofree char *params = g_strdup_printf("('%s',)", a + 8);
		sg_deliver("display_status_ind", params);
	} else if (g_str_has_prefix(a, "run:")) {
		run_for(atoi(a + 4));
	} else if (g_str_has_prefix(a, "wait-attempt:")) {
		/* Until that many attempts have started and the last one is done (at most 10 s) */
		gint64 end = g_get_monotonic_time() + 10 * G_TIME_SPAN_SECOND;
		while ((attempts < atoi(a + 13) || st.busy) && g_get_monotonic_time() < end)
			g_main_context_iteration(NULL, TRUE);
	} else if (!strcmp(a, "state")) {
		print_state();
	} else if (g_str_has_prefix(a, "write:")) {
		g_auto(GStrv) kv = g_strsplit(a + 6, "=", 2);
		g_file_set_contents(kv[0], kv[1], -1, NULL);
	} else if (g_str_has_prefix(a, "mem-errno:")) {
		mem_errno = atoi(a + 10);
	} else if (g_str_has_prefix(a, "alarm:")) {
		alarm_mode = atoi(a + 6);
	} else if (!strcmp(a, "on-during-handshake")) {
		on_during_handshake = 1;
	} else if (!strcmp(a, "no-bus")) {
		sg.no_bus = TRUE;
	} else if (!strcmp(a, "busy")) {
		st.busy = TRUE;
	} else if (g_str_has_prefix(a, "log-after:")) {
		st.log_after_ms = atoi(a + 10);
	} else if (!strcmp(a, "timer")) {
		on_timer(NULL);
		print_state();
	} else if (!strcmp(a, "alarm?")) {
		printf("alarm clock=%d alarm=%d value=%ld.%09ld\n", alarm_clock, alarm_clock == CLOCK_REALTIME_ALARM,
		       (long)alarm_when.it_value.tv_sec, alarm_when.it_value.tv_nsec);
	} else {
		g_error("unknown command %s", a);
	}
	fflush(stdout);
}

static void script_loop(GMainLoop *loop)
{
	/* What the daemon did before the loop: the replies of its start-up calls are delivered */
	while (g_main_context_iteration(NULL, FALSE))
		;
	print_state();
	for (char **a = script; a && *a; a++)
		command(*a);
}

int main(int argc, char **argv)
{
	int n = 1;
	while (n < argc && strcmp(argv[n], "--"))
		n++;
	script = n < argc ? argv + n + 1 : NULL;
	/* Commands before the daemon starts (replies of its start-up calls) */
	while (script && *script && strcmp(*script, "start")) {
		command(*script);
		script++;
	}
	if (script && *script)
		script++;
	argv[n] = NULL;
	cancel_flag = &st.cancel;
	return real_main(n, argv);
}
"""


@pytest.fixture(scope="module")
def service_built(tmp_path_factory):
    d = tmp_path_factory.mktemp("suspend-service")
    sandboxed_source(SRC, d, [
        ("/sys/class/drm/card0-DSI-1/dpms", d / "dpms"),
        ("/sys/class/udc", d / "udc"),
        ("/sys/power/wakeup_count", d / "wakeup_count"),
        ("/sys/power/state", d / "state"),
        ("/sys/kernel/wakeup_reasons/last_resume_reason", d / "resume_reason"),
        ("/sys/class/wakeup", d / "wakeup"),
        ("/run/user/*/plinius-aod", f"{d}/run-user/*/plinius-aod"),
    ])
    exe = build_harness(d, "suspend_service", SERVICE, pkgs=("gio-unix-2.0",))
    return d, functools.partial(run, exe)


@pytest.fixture
def service(service_built):
    d, binary = service_built
    shutil.rmtree(d / "udc", ignore_errors=True)
    shutil.rmtree(d / "run-user", ignore_errors=True)
    shutil.rmtree(d / "wakeup", ignore_errors=True)
    for name in ("dpms", "wakeup_count", "state", "resume_reason"):
        p = d / name
        if p.is_dir():
            p.rmdir()
        p.unlink(missing_ok=True)
    (d / "udc/musb-hdrc").mkdir(parents=True)
    (d / "udc/musb-hdrc/state").write_text("not attached\n")
    (d / "dpms").write_text("Off\n")
    (d / "wakeup_count").write_text("77\n")
    (d / "state").write_text("")
    (d / "resume_reason").write_text("Resume caused by IRQ 9, alarmtimer\n")
    (d / "run-user/1000").mkdir(parents=True)
    return d, binary


LOGIND_OK = "reply:Get org.freedesktop.login1.Manager=(<'shutdown:idle'>,)"
TIMESYNC_ON = ("reply:GetUnitFileState=('enabled',)", "reply:StopUnit=(objectpath '/job/1',)",
               "reply:StartUnit=(objectpath '/job/2',)")


def parse_state(line):
    return dict(x.split("=") for x in line.split()[1:])


def state_lines(out):
    return [parse_state(line) for line in out.splitlines() if line.startswith("state ")]


def test_service_suspends_while_the_display_is_off(service):
    d, binary = service
    r = binary("--off-delay=0", "--awake-delay=1", "--", LOGIND_OK, *TIMESYNC_ON,
               "reply:get_display_status=('on',)", "start",
               "display:off", "wait-attempt:1", "state", "display:on", "run:50", "state")
    out = r.stdout
    # The mce signal is subscribed, the start state asked from plinius-mce
    assert "subscribe com.nokia.mce /com/nokia/mce/signal com.nokia.mce.signal display_status_ind" in out
    assert "call-sync com.nokia.mce /com/nokia/mce/request com.nokia.mce.request get_display_status()" in out
    first, dark, light = state_lines(out)
    assert first["off"] == "0" and first["off_delay"] == "0" and first["awake_delay"] == "1000"
    # One suspend (then the awake delay of 1 s), with timesyncd stopped before it
    assert dark["off"] == "1" and dark["suspends"] == "1" and dark["timesync_stopped"] == "1"
    assert (d / "state").read_text() == "mem"
    assert ("call org.freedesktop.systemd1 /org/freedesktop/systemd1 org.freedesktop.systemd1.Manager "
            "StopUnit('systemd-timesyncd.service', 'replace')") in out
    assert "woken by Resume caused by IRQ 9, alarmtimer" in r.stderr
    # Display on: timesyncd is started again, nothing scheduled, the period is logged
    assert light["off"] == "0" and light["timer"] == "0" and light["timesync_stopped"] == "0"
    # (Started once at start too: the display was on)
    assert out.rindex("StartUnit('systemd-timesyncd.service', 'replace')") > out.index("StopUnit")
    assert "display on: 1 suspends" in r.stderr


def test_service_start_state_from_the_connector(service):
    d, binary = service
    # plinius-mce does not answer: the connector says off, so the first attempt is scheduled
    r = binary("--off-delay=0", "--awake-delay=1", "--", LOGIND_OK, "reply:GetUnitFileState=('disabled',)",
               "start", "wait-attempt:1", "state")
    first, later = state_lines(r.stdout)
    assert first["off"] == "1"
    assert later["suspends"] == "1"
    # Automatic time off (unit disabled): timesyncd is left alone
    assert "StopUnit" not in r.stdout and later["timesync_stopped"] == "0"
    assert (d / "state").read_text() == "mem"


def test_service_restarts_timesync_a_previous_run_stopped(service):
    d, binary = service
    (d / "dpms").write_text("On\n")
    r = binary("--", *TIMESYNC_ON, "start", "run:20")
    # Display on at start: a previous run may have left timesyncd stopped
    assert "call org.freedesktop.systemd1 /org/freedesktop/systemd1 org.freedesktop.systemd1.Manager " \
           "StartUnit('systemd-timesyncd.service', 'replace')" in r.stdout
    assert state_lines(r.stdout)[0]["timesync_stopped"] == "0"


def test_service_failed_timesync_job_is_only_logged(service):
    d, binary = service
    (d / "dpms").write_text("On\n")
    r = binary("--", "reply:GetUnitFileState=('enabled',)", "reply:StartUnit=!job refused", "start", "run:20")
    assert "StartUnit systemd-timesyncd.service: job refused" in r.stderr


def test_service_waits_for_a_sleep_inhibitor(service):
    d, binary = service
    r = binary("--off-delay=0", "--", "reply:Get org.freedesktop.login1.Manager=(<'shutdown:sleep:idle'>,)",
               "reply:GetUnitFileState=('enabled',)", "reply:StopUnit=(objectpath '/j',)",
               "start", "display:off", "run:100", "state")
    last = state_lines(r.stdout)[-1]
    # A call or music holds "sleep": no attempt, looked at again in 10 s, timesyncd untouched
    assert last["suspends"] == "0" and last["timer"] == "1" and last["busy"] == "0"
    assert (d / "state").read_text() == ""
    assert "StopUnit" not in r.stdout
    assert "call-sync org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.DBus.Properties " \
           "Get('org.freedesktop.login1.Manager', 'BlockInhibited')" in r.stdout


def test_service_suspends_when_logind_does_not_answer(service):
    d, binary = service
    r = binary("--off-delay=0", "--awake-delay=1", "--", "start", "display:off", "wait-attempt:1", "state")
    # No answer about inhibitors (nor about timesyncd): suspending is allowed
    assert "logind BlockInhibited: fake bus: no reply for Get" in r.stderr
    assert "systemd-timesyncd.service state: fake bus: no reply" in r.stderr
    assert state_lines(r.stdout)[-1]["suspends"] == "1"
    assert (d / "state").read_text() == "mem"


@pytest.mark.parametrize("err,expected_retry", [(16, "2000"), (11, "2000"), (5, "1000")])
def test_service_refused_and_failed_suspends(service, err, expected_retry):
    d, binary = service
    r = binary("--off-delay=0", "--", LOGIND_OK, f"mem-errno:{err}", "start", "display:off", "wait-attempt:1", "state")
    last = state_lines(r.stdout)[-1]
    # EBUSY/EAGAIN: a wakeup source got active, retried with back-off; anything else: in 10 s
    assert last["suspends"] == "0" and last["timer"] == "1"
    assert last["retry"] == expected_retry
    if err == 5:
        assert "suspend failed: Input/output error" in r.stderr
    else:
        assert "suspend failed" not in r.stderr


def test_service_display_on_during_handshake(service):
    d, binary = service
    r = binary("--off-delay=0", "--", LOGIND_OK, "on-during-handshake", "start", "display:off", "wait-attempt:1",
               "state")
    # Written back, then the display came on: "mem" is never written
    assert (d / "wakeup_count").read_text().startswith("77")
    assert (d / "state").read_text() == ""
    assert state_lines(r.stdout)[-1]["suspends"] == "0"


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read-only file all the same")
def test_service_count_changed_since_read(service):
    d, binary = service
    (d / "wakeup_count").chmod(0o444)     # the write-back fails as with EINVAL
    try:
        r = binary("--off-delay=0", "--", LOGIND_OK, "start", "display:off", "wait-attempt:1", "state")
    finally:
        (d / "wakeup_count").chmod(0o644)
    last = state_lines(r.stdout)[-1]
    assert last["suspends"] == "0" and last["retry"] != "1000"
    assert (d / "state").read_text() == ""


def test_service_always_on_display_alarm(service):
    import time
    d, binary = service
    (d / "run-user/1000/plinius-aod").write_text("")
    before = time.time()
    r = binary("--off-delay=0", "--awake-delay=1", "--", LOGIND_OK, "alarm:1", "start", "display:off",
               "wait-attempt:1", "alarm?", "state")
    k = r.stdout.splitlines()
    alarm = [line for line in k if line.startswith("alarm clock=")][0]
    assert " alarm=1 " in alarm          # CLOCK_REALTIME_ALARM: wakes from suspend
    sec, nsec = alarm.split("value=")[1].split(".")
    # Shortly after the next full minute (300 ms), absolute
    assert int(sec) % 60 == 0 and before < int(sec) <= before + 61
    assert int(nsec) == 300_000_000
    # Closed after the wake-up, so it does not stay armed
    assert "alarm closed" in r.stdout
    assert state_lines(r.stdout)[-1]["suspends"] == "1"


@pytest.mark.parametrize("mode,message", [(0, "alarm timer: Operation not permitted"),
                                          (2, "alarm timer: Invalid argument")])
def test_service_alarm_failure_still_suspends(service, mode, message):
    d, binary = service
    (d / "run-user/1000/plinius-aod").write_text("")
    r = binary("--off-delay=0", "--awake-delay=1", "--", LOGIND_OK, f"alarm:{mode}", "start", "display:off",
               "wait-attempt:1", "state")
    assert message in r.stderr
    assert state_lines(r.stdout)[-1]["suspends"] == "1"
    assert "alarm closed" in r.stdout if mode == 2 else "alarm closed" not in r.stdout


def test_service_timer_does_nothing_while_busy_or_on(service):
    d, binary = service
    r = binary("--off-delay=60", "--", "start", "display:off", "busy", "timer", "display:on", "timer")
    s = state_lines(r.stdout)
    # A worker runs: no second attempt, nothing new scheduled
    assert s[1]["busy"] == "1" and s[1]["timer"] == "0"
    assert s[2]["off"] == "0" and s[2]["timer"] == "0"


def test_service_same_display_state_twice(service):
    d, binary = service
    (d / "dpms").write_text("On\n")
    r = binary("--off-delay=60", "--", "start", "display:on", "state", "display:off", "display:off", "state")
    s = state_lines(r.stdout)
    assert s[1]["off"] == "0" and s[1]["timer"] == "0"
    assert s[2]["off"] == "1" and s[2]["timer"] == "1"


@pytest.mark.parametrize("args,message", [
    (("--off-delay=-1",), "delays must not be negative"),
    (("--awake-delay=-3",), "delays must not be negative"),
    (("--off-delay=soon",), "Cannot parse integer value “soon” for --off-delay"),
])
def test_service_bad_options(service, args, message):
    d, binary = service
    r = binary(*args, "--", "start", check=False)
    assert r.returncode == 2
    assert message in r.stderr


def test_service_needs_the_system_bus(service_built):
    d, binary = service_built
    # Fatal at start: systemd restarts the service
    r = binary("--", "no-bus", "start", check=False)
    assert r.returncode == 99
    assert "system bus: fake bus: no system bus" in r.stderr


def blocker_messages(stderr):
    return [line.split("plinius-suspend")[-1] for line in stderr.splitlines()
            if "not suspending" in line or "no longer blocks" in line]


# Display on at start: the dark phase begins with the signal, after log-after: is set
ON_AT_START = "reply:get_display_status=('on',)"
INHIBITORS = ("reply:ListInhibitors=([('sleep:idle', 'gnome-calls', 'Call in progress', 'block', 1000, 812),"
              " ('sleep', 'NetworkManager', 'turn off networks', 'delay', 0, 7),"
              " ('shutdown', 'phosh', 'power key', 'block', 1000, 9)],)")


def test_service_names_the_sleep_inhibitor(service):
    d, binary = service
    r = binary("--off-delay=0", "--awake-delay=1", "--",
               "reply:Get org.freedesktop.login1.Manager=(<'shutdown:sleep:idle'>,)", INHIBITORS, ON_AT_START,
               "start", "log-after:0", "display:off", "run:50", LOGIND_OK, "run:10100", "state")
    msgs = blocker_messages(r.stderr)
    # Only block inhibitors of "sleep" are named; logged once, and its end when it goes
    assert any("not suspending for 0 s: sleep inhibitor gnome-calls (Call in progress, pid 812)" in m
               for m in msgs)
    assert not any("NetworkManager" in m or "phosh" in m for m in msgs)
    assert sum("not suspending" in m for m in msgs) == 1
    assert any("sleep inhibitor gnome-calls (Call in progress, pid 812) no longer blocks, after" in m for m in msgs)
    assert state_lines(r.stdout)[-1]["suspends"] == "1"


def test_service_unnamed_inhibitor_and_short_blocks(service):
    d, binary = service
    blocked = "reply:Get org.freedesktop.login1.Manager=(<'sleep'>,)"
    # logind does not list it: still logged as a sleep inhibitor
    r = binary("--off-delay=0", "--", blocked, ON_AT_START, "start", "log-after:0", "display:off", "run:50")
    assert any("not suspending for 0 s: sleep inhibitor (unnamed)" in m for m in blocker_messages(r.stderr))
    # Shorter than the log delay (30 s by default): nothing in the log, also not its end
    r = binary("--off-delay=0", "--", blocked, INHIBITORS, ON_AT_START, "start", "display:off", "run:50", "display:on",
               "run:20")
    assert blocker_messages(r.stderr) == []


def test_service_names_usb_host_and_connector(service):
    d, binary = service
    (d / "udc/musb-hdrc/state").write_text("configured\n")
    r = binary("--off-delay=0", "--", LOGIND_OK, ON_AT_START, "start", "log-after:0", "display:off", "run:50",
               "display:on", "run:20")
    msgs = blocker_messages(r.stderr)
    assert any("not suspending for 0 s: a USB host is connected" in m for m in msgs)
    # The display coming on ends it
    assert any("a USB host is connected no longer blocks, after" in m for m in msgs)
    (d / "udc/musb-hdrc/state").write_text("not attached\n")
    (d / "dpms").write_text("On\n")
    r = binary("--off-delay=0", "--", LOGIND_OK, ON_AT_START, "start", "log-after:0",
               "display:off", "run:50")
    assert any("not suspending for 0 s: the display connector is still on" in m
               for m in blocker_messages(r.stderr))


def test_service_names_wakeup_sources_when_the_count_read_blocks(service):
    d, binary = service
    # A FIFO without writer: the read blocks as wakeup_count does while an event is in progress
    (d / "wakeup_count").unlink()
    os.mkfifo(d / "wakeup_count")
    (d / "wakeup/wakeup2").mkdir(parents=True)
    (d / "wakeup/wakeup2/name").write_text("bt_drv_io\n")
    (d / "wakeup/wakeup2/active_time_ms").write_text("3000\n")
    r = binary("--off-delay=0", "--", LOGIND_OK, ON_AT_START, "start", "log-after:50", "display:off", "run:200", "state")
    assert "wakeup events in progress for 0 s, active wakeup sources: bt_drv_io (3 s)" in r.stderr
    assert state_lines(r.stdout)[-1]["busy"] == "1"
