# plinius-assist: short, double and long presses of the assistant button (timers faked, so the
# timing is exact), the flip cover, key events from the input device, the torch level and the
# custom command line. GSettings uses the package's schema with the memory backend; D-Bus calls
# and spawning are printed instead of done.

import functools
import shutil
import struct
import subprocess

import pytest

from dhw_harness import PKG, build_harness, run, sandboxed_source

SRC = PKG / "plinius-assist" / "plinius-assist.c"
SCHEMA = PKG / "plinius-assist" / "org.plinius.assistant-button.gschema.xml"

HARNESS = r"""
#define _GNU_SOURCE
#include <unistd.h>
#include <glib.h>
#include <gio/gio.h>
#include <gio/gdesktopappinfo.h>
#include <stdio.h>
#include <stdlib.h>

/* Timers: a fake clock, advanced by the test */
typedef struct { guint id; gint64 due; GSourceFunc fn; gpointer data; } Timer;
static GArray *timers;
static gint64 now_ms;
static guint next_id = 100;
static guint fake_timeout_add(guint ms, GSourceFunc fn, gpointer data)
{
	if (!timers)
		timers = g_array_new(FALSE, FALSE, sizeof(Timer));
	Timer t = {next_id++, now_ms + ms, fn, data};
	g_array_append_val(timers, t);
	return t.id;
}
static gboolean fake_source_remove(guint id)
{
	for (guint i = 0; timers && i < timers->len; i++)
		if (g_array_index(timers, Timer, i).id == id) {
			g_array_remove_index(timers, i);
			return TRUE;
		}
	return FALSE;
}
static void advance(gint64 ms)
{
	gint64 end = now_ms + ms;
	for (;;) {
		int best = -1;
		for (guint i = 0; timers && i < timers->len; i++)
			if (g_array_index(timers, Timer, i).due <= end &&
			    (best < 0 || g_array_index(timers, Timer, i).due < g_array_index(timers, Timer, best).due))
				best = i;
		if (best < 0)
			break;
		Timer t = g_array_index(timers, Timer, best);
		g_array_remove_index(timers, best);
		now_ms = t.due;
		t.fn(t.data);
	}
	now_ms = end;
}

static void fake_call(GDBusConnection *c, const char *dest, const char *path, const char *iface,
		      const char *method, GVariant *args, const GVariantType *type, GDBusCallFlags flags,
		      int timeout, GCancellable *cancel, GAsyncReadyCallback cb, gpointer data)
{
	gchar *text = args ? g_variant_print(g_variant_ref_sink(args), FALSE) : g_strdup("()");
	printf("at %" G_GINT64_FORMAT " call %s.%s%s\n", now_ms, iface, method, text);
	g_free(text);
	if (args)
		g_variant_unref(args);
}
static gboolean fake_spawn(const char *dir, char **argv, char **envp, GSpawnFlags flags, GSpawnChildSetupFunc f,
			   gpointer data, GPid *pid, GError **error)
{
	printf("at %" G_GINT64_FORMAT " spawn", now_ms);
	for (char **a = argv; *a; a++)
		printf(" [%s]", *a);
	printf("\n");
	return TRUE;
}
#define g_timeout_add fake_timeout_add
#define g_source_remove fake_source_remove
#define g_dbus_connection_call fake_call
#define g_spawn_async fake_spawn
#define main real_main
#include "plinius-assist.c"
#undef main

int main(int argc, char **argv)
{
	self.settings = g_settings_new(SCHEMA);
	for (int i = 1; i < argc; i++) {
		g_auto(GStrv) a = g_strsplit(argv[i], "=", 2);
		if (!strcmp(a[0], "down")) {
			on_key(TRUE);
		} else if (!strcmp(a[0], "up")) {
			on_key(FALSE);
		} else if (!strcmp(a[0], "wait")) {
			advance(atoi(a[1]));
		} else if (!strcmp(a[0], "cover")) {
			on_cover(atoi(a[1]));
		} else if (!strcmp(a[0], "events")) {
			int fd = open(a[1], O_RDONLY);
			on_input(fd, G_IO_IN, NULL);
			close(fd);
		} else if (!strcmp(a[0], "torch-level")) {
			printf("torch-level=%d\n", torch_brightness(atoi(a[1])));
		} else if (g_str_has_prefix(a[0], "set:")) {
			/* set:<key>=<GVariant text> */
			GVariant *v = g_variant_parse(NULL, a[1], NULL, NULL, NULL);
			if (!g_settings_set_value(self.settings, a[0] + 4, v))
				printf("cannot set %s\n", a[0] + 4);
		} else if (!strcmp(a[0], "torch")) {
			toggle_torch();
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("assist")
    if shutil.which("glib-compile-schemas") is None:
        pytest.skip("no glib-compile-schemas")
    schemas = d / "schemas"
    schemas.mkdir()
    shutil.copy(SCHEMA, schemas)
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    sandboxed_source(SRC, d, [("/dev/input", d / "input"), ("/sys/class/leds", d / "leds")])
    exe = build_harness(d, "assist", HARNESS, pkgs=("gio-unix-2.0",))
    env = {"GSETTINGS_SCHEMA_DIR": str(schemas), "GSETTINGS_BACKEND": "memory",
           "XDG_DATA_DIRS": str(d / "nodata"), "HOME": str(d / "home"), "PATH": "/usr/bin:/bin"}
    return d, functools.partial(run, exe, env=env)


@pytest.fixture
def assist(built):
    d, binary = built
    shutil.rmtree(d / "leds", ignore_errors=True)
    return d, binary


CUSTOM = ["set:short-press='custom'", "set:short-press-command='short'",
          "set:long-press='custom'", "set:long-press-command='long'",
          "set:double-press-command='double'"]


def actions(out):
    return [line for line in out.splitlines() if line.startswith("at ")]


def test_short_press_acts_at_once_without_double_action(assist):
    d, binary = assist
    out = binary(*CUSTOM, "down", "wait=100", "up", "wait=1000").stdout
    assert actions(out) == ["at 100 spawn [short]"]


def test_short_press_waits_for_a_possible_second(assist):
    d, binary = assist
    out = binary(*CUSTOM, "set:double-press='custom'", "down", "wait=100", "up", "wait=349").stdout
    assert actions(out) == []
    out = binary(*CUSTOM, "set:double-press='custom'", "down", "wait=100", "up", "wait=350").stdout
    assert actions(out) == ["at 450 spawn [short]"]


def test_double_press(assist):
    d, binary = assist
    out = binary(*CUSTOM, "set:double-press='custom'", "down", "wait=80", "up", "wait=200", "down",
                 "wait=80", "up", "wait=1000").stdout
    assert actions(out) == ["at 360 spawn [double]"]


def test_long_press(assist):
    d, binary = assist
    out = binary(*CUSTOM, "set:double-press='custom'", "down", "wait=599").stdout
    assert actions(out) == []
    out = binary(*CUSTOM, "set:double-press='custom'", "down", "wait=600", "wait=500", "up",
                 "wait=1000").stdout
    # Fires while held; releasing it afterwards does nothing more
    assert actions(out) == ["at 600 spawn [long]"]


def test_autorepeat_is_no_new_press(assist):
    d, binary = assist
    out = binary(*CUSTOM, "down", "wait=100", "down", "wait=100", "down", "wait=100", "up",
                 "wait=1000").stdout
    assert actions(out) == ["at 300 spawn [short]"]


def test_long_then_short(assist):
    d, binary = assist
    out = binary(*CUSTOM, "down", "wait=700", "up", "wait=100", "down", "wait=50", "up").stdout
    assert actions(out) == ["at 600 spawn [long]", "at 850 spawn [short]"]


def test_custom_command_quoting(assist):
    d, binary = assist
    out = binary("set:short-press='custom'",
                 "set:short-press-command='/opt/my\\\\ tool --name \"a b\" \\'c\\''", "down", "up").stdout
    assert actions(out) == ["at 0 spawn [/opt/my tool] [--name] [a b] [c]"]


def test_custom_without_command(assist):
    d, binary = assist
    assert actions(binary("set:short-press='custom'", "down", "up").stdout) == []


def test_lock_screen(assist):
    d, binary = assist
    out = binary("set:short-press='lock'", "down", "up").stdout
    assert actions(out) == ["at 0 call org.gnome.ScreenSaver.Lock()"]


def test_cover(assist):
    d, binary = assist
    out = binary("cover=1", "cover=1", "cover=0").stdout
    assert actions(out) == ["at 0 call org.gnome.ScreenSaver.SetActive(true,)",
                            "at 0 call org.gnome.ScreenSaver.SetActive(false,)"]
    out = binary("set:cover=false", "cover=1", "cover=0").stdout
    assert actions(out) == []


def input_event(etype, code, value):
    return struct.pack("llHHi", 0, 0, etype, code, value)


EV_KEY, EV_SYN = 1, 0
KEY_ASSISTANT, KEY_F2, KEY_MACRO1, KEY_VOLUMEUP = 0x247, 60, 0x290, 115


def test_key_events_from_the_device(assist):
    d, binary = assist
    ev = d / "events"
    ev.write_bytes(b"".join([
        input_event(EV_KEY, KEY_VOLUMEUP, 1),       # other keys: ignored
        input_event(EV_KEY, KEY_ASSISTANT, 1), input_event(EV_SYN, 0, 0),
        input_event(EV_KEY, KEY_ASSISTANT, 2),      # autorepeat value 2 counts as down
        input_event(EV_KEY, KEY_ASSISTANT, 0), input_event(EV_SYN, 0, 0),
        input_event(EV_KEY, KEY_MACRO1, 1),
        input_event(EV_KEY, KEY_F2, 0),             # without the hwdb mapping
    ]))
    out = binary(*CUSTOM, f"events={ev}").stdout
    assert actions(out) == ["at 0 spawn [short]",
                            "at 0 call org.gnome.ScreenSaver.SetActive(true,)",
                            "at 0 call org.gnome.ScreenSaver.SetActive(false,)"]


def torch_led(d, brightness, maximum):
    led = d / "leds/white:torch"
    led.mkdir(parents=True)
    (led / "brightness").write_text(f"{brightness}\n")
    (led / "max_brightness").write_text(f"{maximum}\n")


def test_torch_toggle(assist):
    d, binary = assist
    torch_led(d, 0, 10)
    # No org.plinius.torch schema here: the brightest level
    assert actions(binary("torch").stdout) == [
        "at 0 call org.freedesktop.login1.Session.SetBrightness('leds', 'white:torch', 10)"]
    shutil.rmtree(d / "leds")
    torch_led(d, 4, 10)
    assert actions(binary("torch").stdout) == [
        "at 0 call org.freedesktop.login1.Session.SetBrightness('leds', 'white:torch', 0)"]


def test_torch_missing_led(assist):
    d, binary = assist
    assert actions(binary("torch").stdout) == []


# ---- The service as it runs: main(), the key device, the actions over a scripted bus ----
#
# fakes/dhw_scripted_gdbus.h answers the D-Bus calls (reply:<Method>=<GVariant text>, a text
# starting with '!' is an error). The input devices are files in tmp: the events in the file
# are read, then its end is the device going away. Their key capabilities (EVIOCGBIT) and the
# keys held at open (EVIOCGKEY) come from a fake ioctl, out of caps/<name>.keys and
# caps/<name>.held ("fail": the ioctl fails). Timers run on a fake clock (wait=<ms>), the main
# loop runs the commands given after the binary; drain lets it run until nothing is ready.
# FAKE_NO_BUS=session|system: that bus is not there. Programs started (the camera app, custom
# commands) are stubs in tmp.

SERVICE = r"""
#define G_DISABLE_CAST_CHECKS
#include <errno.h>
#include <fcntl.h>
#include <glib.h>
#include <gio/gio.h>
#include <gio/gdesktopappinfo.h>
#include <glib-unix.h>
#include <linux/input.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>
#include "dhw_scripted_gdbus.h"

static GDBusConnection *fake_bus_get_sync(GBusType type, GCancellable *cancel, GError **error)
{
	const char *missing = getenv("FAKE_NO_BUS");
	const char *name = type == G_BUS_TYPE_SYSTEM ? "system" : "session";
	sg_init();
	if (missing && !strcmp(missing, name)) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND, "fake bus: no %s bus", name);
		return NULL;
	}
	return sg.bus;
}
#undef g_bus_get_sync
#define g_bus_get_sync fake_bus_get_sync

/* Timers on a fake clock */
typedef struct { guint id; gint64 due; guint interval; GSourceFunc fn; gpointer data; } Timer;
static GArray *timers;
static gint64 now_ms;
static guint next_id = 100;
static guint fake_timeout_add(guint ms, GSourceFunc fn, gpointer data)
{
	if (!timers)
		timers = g_array_new(FALSE, FALSE, sizeof(Timer));
	Timer t = {next_id++, now_ms + ms, ms, fn, data};
	g_array_append_val(timers, t);
	return t.id;
}
static guint fake_timeout_add_seconds(guint s, GSourceFunc fn, gpointer data)
{
	return fake_timeout_add(s * 1000, fn, data);
}
static gboolean fake_source_remove(guint id)
{
	for (guint i = 0; timers && i < timers->len; i++)
		if (g_array_index(timers, Timer, i).id == id) {
			g_array_remove_index(timers, i);
			return TRUE;
		}
	return FALSE;
}
static void drain(void)
{
	while (g_main_context_iteration(NULL, FALSE))
		;
}
static void advance(gint64 ms)
{
	gint64 end = now_ms + ms;
	for (;;) {
		int best = -1;
		for (guint i = 0; timers && i < timers->len; i++)
			if (g_array_index(timers, Timer, i).due <= end &&
			    (best < 0 || g_array_index(timers, Timer, i).due < g_array_index(timers, Timer, best).due))
				best = i;
		if (best < 0)
			break;
		Timer t = g_array_index(timers, Timer, best);
		now_ms = t.due;
		/* A timer that returns CONTINUE runs again after its interval */
		gboolean again = t.fn(t.data);
		for (guint i = 0; i < timers->len; i++)
			if (g_array_index(timers, Timer, i).id == t.id) {
				if (again)
					g_array_index(timers, Timer, i).due += t.interval;
				else
					g_array_remove_index(timers, i);
				break;
			}
	}
	now_ms = end;
}

/* The input devices: which path each fd is */
static char *fd_path[1024];
static int fake_open(const char *path, int flags)
{
	int fd = open(path, flags);
	if (fd >= 0 && fd < 1024) {
		g_free(fd_path[fd]);
		fd_path[fd] = g_strdup(path);
	}
	return fd;
}
static int caps_bits(int fd, const char *ext, unsigned long *bits, size_t len)
{
	g_autofree char *name = g_path_get_basename(fd_path[fd]);
	g_autofree char *caps = g_strdup_printf("%s/%s.%s", getenv("FAKE_CAPS"), name, ext);
	g_autofree char *text = NULL;
	if (!g_file_get_contents(caps, &text, NULL, NULL) || g_str_has_prefix(text, "fail")) {
		errno = ENOTTY;
		return -1;
	}
	memset(bits, 0, len);
	g_auto(GStrv) codes = g_strsplit_set(g_strstrip(text), " ", -1);
	for (char **c = codes; *c; c++)
		if (**c) {
			unsigned code = strtoul(*c, NULL, 0);
			bits[code / (8 * sizeof(long))] |= 1UL << (code % (8 * sizeof(long)));
		}
	return 0;
}
static int fake_ioctl(int fd, unsigned long request, void *arg)
{
	size_t len = _IOC_SIZE(request);
	if (request == EVIOCGBIT(EV_KEY, len))
		return caps_bits(fd, "keys", arg, len);
	if (request == EVIOCGKEY(len))
		return caps_bits(fd, "held", arg, len);
	errno = EINVAL;
	return -1;
}
static gboolean quit_called;
static void fake_quit(GMainLoop *loop)
{
	printf("quit\n");
	quit_called = TRUE;
	g_main_loop_quit(loop);
}

static void script_loop(GMainLoop *loop);
#define g_main_loop_run script_loop
#define g_main_loop_quit fake_quit
#define g_timeout_add fake_timeout_add
#define g_timeout_add_seconds fake_timeout_add_seconds
#define g_source_remove fake_source_remove
#define open fake_open
#define ioctl fake_ioctl
#define main real_main
#include "plinius-assist.c"
#undef main
#undef open
#undef g_main_loop_run
#undef g_main_loop_quit

static char **script;
/* FAKE_FIFO=<name>: that device is a FIFO kept open for writing, a device that stays */
static int fifo_writer = -1;

static void command(const char *arg)
{
	g_auto(GStrv) a = g_strsplit(arg, "=", 2);
	if (!strcmp(a[0], "drain")) {
		drain();
	} else if (!strcmp(a[0], "wait")) {
		advance(atoi(a[1]));
	} else if (g_str_has_prefix(a[0], "reply:")) {
		sg_reply(a[0] + 6, a[1]);
	} else if (g_str_has_prefix(a[0], "set:")) {
		g_autoptr(GVariant) v = g_variant_ref_sink(g_variant_parse(NULL, a[1], NULL, NULL, NULL));
		if (!g_settings_set_value(self.settings, a[0] + 4, v))
			printf("cannot set %s\n", a[0] + 4);
	} else if (!strcmp(a[0], "torch-level")) {
		g_autoptr(GSettings) torch = g_settings_new(TORCH_SCHEMA);
		g_settings_set_int(torch, "brightness", atoi(a[1]));
	} else if (!strcmp(a[0], "press")) {
		/* A short press as the key device would deliver it */
		on_key(TRUE);
		on_key(FALSE);
		drain();
	} else if (!strcmp(a[0], "down") || !strcmp(a[0], "up")) {
		on_key(!strcmp(a[0], "down"));
		drain();
	} else if (!strcmp(a[0], "rename")) {
		g_auto(GStrv) p = g_strsplit(a[1], ":", 2);
		if (rename(p[0], p[1]) < 0)
			abort();
	} else if (!strcmp(a[0], "term")) {
		/* GLib hands the signal to the main loop from its own thread */
		raise(SIGTERM);
		while (!quit_called)
			g_main_context_iteration(NULL, TRUE);
	} else if (!strcmp(a[0], "feed")) {
		/* The events in that file come from the live device */
		g_autofree char *data = NULL;
		gsize len;
		if (!g_file_get_contents(a[1], &data, &len, NULL) || write(fifo_writer, data, len) != (ssize_t)len)
			abort();
		drain();
	} else if (!strcmp(a[0], "unplug")) {
		close(fifo_writer);
		drain();
	} else if (!strcmp(a[0], "state")) {
		printf("state at %" G_GINT64_FORMAT " device=%s cover=%d rescan=%d\n", now_ms,
		       self.fd >= 0 ? fd_path[self.fd] : "-", self.cover_closed, self.rescan_source != 0);
	} else {
		printf("unknown command %s\n", arg);
		exit(98);
	}
	fflush(stdout);
}

static void script_loop(GMainLoop *loop)
{
	for (char **a = script; a && *a; a++)
		command(*a);
}

int main(int argc, char **argv)
{
	script = argv + 1;
	if (getenv("FAKE_FIFO")) {
		g_autofree char *path = g_build_filename(INPUT_DIR, getenv("FAKE_FIFO"), NULL);
		if (mkfifo(path, 0600) < 0 || (fifo_writer = open(path, O_RDWR | O_CLOEXEC)) < 0)
			abort();
	}
	int r = real_main();
	printf("exit %d fd-open=%d\n", r, self.fd >= 0 && fcntl(self.fd, F_GETFD) >= 0);
	return r;
}
"""

TORCH_SCHEMA = PKG / "plinius-torch" / "org.plinius.torch.gschema.xml"


@pytest.fixture(scope="module")
def service_built(tmp_path_factory):
    d = tmp_path_factory.mktemp("assist-service")
    if shutil.which("glib-compile-schemas") is None:
        pytest.skip("no glib-compile-schemas")
    for name, schemas in (("schemas", [SCHEMA]), ("schemas-torch", [SCHEMA, TORCH_SCHEMA])):
        (d / name).mkdir()
        for s in schemas:
            shutil.copy(s, d / name)
        subprocess.run(["glib-compile-schemas", str(d / name)], check=True)
    sandboxed_source(SRC, d, [("/dev/input", d / "input"), ("/sys/class/leds", d / "leds")])
    exe = build_harness(d, "assist-service", SERVICE, pkgs=("gio-unix-2.0",))
    return d, exe


@pytest.fixture
def service(service_built, tmp_path):
    d, exe = service_built
    for sub in ("input", "caps", "leds", "home", "data", "config"):
        shutil.rmtree(d / sub, ignore_errors=True)
        (d / sub).mkdir()
    (d / "data/applications").mkdir()
    log = d / "spawned.log"
    log.write_text("")

    def start(*script, torch_schema=False, no_bus=None, fifo=None, check=True):
        env = {"GSETTINGS_SCHEMA_DIR": str(d / ("schemas-torch" if torch_schema else "schemas")),
               "GSETTINGS_BACKEND": "memory", "XDG_DATA_DIRS": str(d / "data"),
               "XDG_DATA_HOME": str(d / "data-home"), "XDG_CONFIG_HOME": str(d / "config"),
               "HOME": str(d / "home"), "PATH": f"{d / 'bin'}:/usr/bin:/bin",
               "FAKE_CAPS": str(d / "caps"), "SPAWN_LOG": str(log),
               # GIO announces launched apps on the session bus: there is none
               "DBUS_SESSION_BUS_ADDRESS": f"unix:path={d / 'no-bus'}"}
        if no_bus:
            env["FAKE_NO_BUS"] = no_bus
        if fifo:
            env["FAKE_FIFO"] = fifo
        return run(exe, *script, env=env, check=check)
    return d, start


def device(d, name, events=b"", keys=(KEY_ASSISTANT,), held=()):
    (d / "input" / name).write_bytes(events)
    if keys is not None:
        (d / "caps" / f"{name}.keys").write_text(keys if isinstance(keys, str) else
                                                 " ".join(map(str, keys)))
    if held is not None:
        (d / "caps" / f"{name}.held").write_text(held if isinstance(held, str) else
                                                 " ".join(map(str, held)))


def stub(d, name, script):
    (d / "bin").mkdir(exist_ok=True)
    p = d / "bin" / name
    p.write_text(script)
    p.chmod(0o755)
    return p


def bus_lines(out):
    return [line for line in out.splitlines() if line.startswith(("call ", "call-sync "))]


def test_service_without_session_bus_exits(service):
    d, start = service
    r = start(no_bus="session", check=False)
    assert r.returncode == 1
    assert "No D-Bus: fake bus: no session bus" in r.stderr


def test_service_without_system_bus_exits(service):
    d, start = service
    r = start(no_bus="system", check=False)
    assert r.returncode == 1
    assert "No D-Bus: fake bus: no system bus" in r.stderr


def test_service_finds_the_device_with_the_assistant_key(service):
    d, start = service
    device(d, "event0", keys=(KEY_VOLUMEUP,))        # another key device
    device(d, "event1", keys="fail")                  # no answer to EVIOCGBIT
    device(d, "event3")
    (d / "input/event2").write_bytes(b"")
    (d / "input/event2").chmod(0)                     # cannot be opened
    device(d, "mouse0")                               # not an event device
    out = start("state", "term").stdout
    assert f"state at 0 device={d}/input/event3 cover=0 rescan=0" in out
    assert out.splitlines()[-2:] == ["quit", "exit 0 fd-open=0"]


def test_service_other_devices_are_not_taken(service):
    d, start = service
    device(d, "event0", keys=(KEY_VOLUMEUP, KEY_F2))
    device(d, "event1", keys="fail")
    device(d, "event2", keys=None)
    r = start("state", "term")
    assert "state at 0 device=- cover=0 rescan=1" in r.stdout
    assert "No assistant button yet" in r.stderr


def test_service_cover_state_at_open_is_taken_as_it_is(service):
    d, start = service
    # The cover is closed when the device is opened: no blanking for it, but opening it acts
    device(d, "event0", held=(KEY_MACRO1,), events=b"".join([
        input_event(EV_KEY, KEY_MACRO1, 1), input_event(EV_KEY, KEY_MACRO1, 0)]))
    out = start("state", "reply:SetActive=()", "drain").stdout
    assert "cover=1" in out.splitlines()[0]
    assert bus_lines(out) == [
        "call org.gnome.ScreenSaver /org/gnome/ScreenSaver org.gnome.ScreenSaver SetActive(false,)"]


def test_service_held_keys_unreadable_counts_as_open_cover(service):
    d, start = service
    device(d, "event0", held="fail", events=input_event(EV_KEY, KEY_F2, 1))
    out = start("state", "reply:SetActive=()", "drain").stdout
    assert "cover=0" in out.splitlines()[0]
    assert bus_lines(out) == [
        "call org.gnome.ScreenSaver /org/gnome/ScreenSaver org.gnome.ScreenSaver SetActive(true,)"]


def test_service_device_gone_looks_again_every_5_s(service):
    d, start = service
    device(d, "event0", events=b"")
    # The staged device appears only after the first rescan
    device(d, "staged", keys=(KEY_ASSISTANT,))
    r = start("drain", "state", "rename=" + f"{d}/input/event0:{d}/gone",
              "wait=4999", "state", "wait=1", "state",
              "rename=" + f"{d}/input/staged:{d}/input/event5", "rename=" +
              f"{d}/caps/staged.keys:{d}/caps/event5.keys", "wait=4999", "state", "wait=1", "state",
              "drain", "state")
    lines = [line for line in r.stdout.splitlines() if line.startswith("state")]
    assert lines == [
        # The end of the file: the device is gone, a rescan is due in 5 s
        "state at 0 device=- cover=0 rescan=1",
        "state at 4999 device=- cover=0 rescan=1",
        # Nothing found at 5 s: again 5 s later
        "state at 5000 device=- cover=0 rescan=1",
        "state at 9999 device=- cover=0 rescan=1",
        f"state at 10000 device={d}/input/event5 cover=0 rescan=0",
        # This one ends at once too (its file is empty)
        "state at 10000 device=- cover=0 rescan=1",
    ]
    assert f"Assistant button on {d}/input/event5" in r.stderr
    assert "Key device gone" in r.stderr


def test_service_without_input_directory_waits_for_the_device(service):
    d, start = service
    shutil.rmtree(d / "input")
    r = start("state", "term")
    assert "state at 0 device=- cover=0 rescan=1" in r.stdout
    assert "No assistant button yet, looking again every 5 s" in r.stderr
    assert r.stdout.splitlines()[-1] == "exit 0 fd-open=0"


def test_service_key_presses_from_the_device_run_the_action(service):
    d, start = service
    device(d, "event0", events=b"".join([
        input_event(EV_KEY, KEY_ASSISTANT, 1), input_event(EV_KEY, KEY_ASSISTANT, 0)]))
    out = start("set:short-press='lock'", "reply:Lock=()", "drain").stdout
    assert bus_lines(out) == [
        "call org.gnome.ScreenSaver /org/gnome/ScreenSaver org.gnome.ScreenSaver Lock()"]


def test_service_failed_call_is_a_warning(service):
    d, start = service
    device(d, "event0")
    r = start("set:short-press='lock'", "reply:Lock=!Access denied", "press", "term")
    assert "Locking the screen failed: Access denied" in r.stderr
    assert r.returncode == 0


def test_service_action_none_does_nothing(service):
    d, start = service
    device(d, "event0")
    r = start("set:short-press='none'", "press", "term")
    assert bus_lines(r.stdout) == []
    assert "short-press" in r.stderr


def test_service_torch_from_a_press_uses_the_torch_level(service):
    d, start = service
    device(d, "event0")
    torch_led(d, 0, 20)
    out = start("set:short-press='torch'", "torch-level=7", "press", "torch-level=0", "press",
                "torch-level=21", "press", "reply:SetBrightness=()", torch_schema=True).stdout
    # The level picked in Phosh's slider; none (0) or one above the LED's maximum: the brightest
    assert [line.split("SetBrightness")[1] for line in bus_lines(out)] == [
        "('leds', 'white:torch', 7)", "('leds', 'white:torch', 20)", "('leds', 'white:torch', 20)"]
    assert bus_lines(out)[0].startswith(
        "call org.freedesktop.login1 /org/freedesktop/login1/session/auto org.freedesktop.login1.Session ")


def test_service_screenshot_goes_to_the_pictures_folder(service):
    d, start = service
    device(d, "event0")
    (d / "config/user-dirs.dirs").write_text('XDG_PICTURES_DIR="$HOME/Bilder"\n')
    out = start("set:short-press='screenshot'", "reply:Screenshot=(true, 'x')", "press").stdout
    (call,) = bus_lines(out)
    prefix = ("call org.gnome.Shell.Screenshot /org/gnome/Shell/Screenshot org.gnome.Shell.Screenshot "
              f"Screenshot(false, true, '{d}/home/Bilder/Screenshots/Screenshot from ")
    assert call.startswith(prefix), call
    import re
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d-\d\d-\d\d\.png'\)", call[len(prefix):]), call
    # Phosh writes the file, but the folder must exist
    assert (d / "home/Bilder/Screenshots").is_dir()


def test_service_screenshot_without_pictures_folder_goes_home(service):
    d, start = service
    device(d, "event0")
    out = start("set:short-press='screenshot'", "reply:Screenshot=(true, 'x')", "press").stdout
    assert f"'{d}/home/Screenshots/Screenshot from " in bus_lines(out)[0]
    assert (d / "home/Screenshots").is_dir()


def test_service_camera_app_is_started(service):
    d, start = service
    device(d, "event0")
    cam = stub(d, "snapshot-stub", '#!/bin/sh\necho "camera $*" >> "$SPAWN_LOG"\n')
    (d / "data/applications/org.gnome.Snapshot.desktop").write_text(
        f"[Desktop Entry]\nType=Application\nName=Camera\nExec={cam}\n")
    r = start("set:short-press='camera'", "press", "term")
    assert "Starting the camera failed" not in r.stderr
    import time
    end = time.monotonic() + 5
    while time.monotonic() < end and "camera" not in (d / "spawned.log").read_text():
        time.sleep(0.02)
    assert (d / "spawned.log").read_text() == "camera \n"


def test_service_camera_app_missing_or_failing(service):
    d, start = service
    device(d, "event0")
    r = start("set:short-press='camera'", "press", "term")
    assert "No camera app org.gnome.Snapshot.desktop" in r.stderr
    # The program exists, but cannot be executed (its interpreter is missing)
    # The app is there, but it cannot be started (its working directory is missing)
    cam = stub(d, "snapshot-stub", '#!/bin/sh\necho "camera $*" >> "$SPAWN_LOG"\n')
    (d / "data/applications/org.gnome.Snapshot.desktop").write_text(
        f"[Desktop Entry]\nType=Application\nName=Camera\nExec={cam}\nPath={d}/missing\n")
    r = start("set:short-press='camera'", "press", "term")
    assert "Starting the camera failed: Failed to change to directory" in r.stderr
    assert (d / "spawned.log").read_text() == ""


@pytest.mark.parametrize("profile,new", [("full", "silent"), ("silent", "full"), ("quiet", "full")])
def test_service_silent_mode_switches_feedbackd(service, profile, new):
    d, start = service
    device(d, "event0")
    out = start("set:short-press='silent'", f"reply:Get org.sigxcpu.Feedback=(<'{profile}'>,)",
                "reply:Set=()", "press").stdout
    assert bus_lines(out) == [
        "call-sync org.sigxcpu.Feedback /org/sigxcpu/Feedback org.freedesktop.DBus.Properties "
        "Get('org.sigxcpu.Feedback', 'Profile')",
        "call org.sigxcpu.Feedback /org/sigxcpu/Feedback org.freedesktop.DBus.Properties "
        f"Set('org.sigxcpu.Feedback', 'Profile', <'{new}'>)",
    ]


def test_service_silent_mode_without_feedbackd(service):
    d, start = service
    device(d, "event0")
    r = start("set:short-press='silent'", "press")
    assert "Reading the feedback profile failed: fake bus: no reply for Get" in r.stderr
    assert not [line for line in bus_lines(r.stdout) if line.startswith("call ")]


def test_service_custom_commands(service):
    d, start = service
    device(d, "event0")
    stub(d, "my-action", '#!/bin/sh\necho "ran $* in $PWD" >> "$SPAWN_LOG"\n')
    r = start("set:short-press='custom'", "set:short-press-command='no-such-program'",
              "set:long-press='custom'", "set:long-press-command='my-action --now'",
              "set:double-press='custom'", "set:double-press-command='broken \"quote'",
              "press", "wait=100", "press",            # a double press
              "wait=1000", "press", "wait=400",        # a short one
              "down", "wait=600", "up", "term")        # a long one
    assert 'Running "broken "quote" failed: Text ended before matching quote was found' in r.stderr
    assert 'Running "no-such-program" failed: Failed to execute child process' in r.stderr
    import time
    end = time.monotonic() + 5
    # Found in PATH, run in the home directory
    while time.monotonic() < end and not (d / "spawned.log").read_text():
        time.sleep(0.02)
    assert (d / "spawned.log").read_text() == f"ran --now in {d}/home\n"


def test_service_live_device_until_unplugged(service):
    d, start = service
    (d / "caps/event4.keys").write_text(str(KEY_ASSISTANT))
    (d / "caps/event4.held").write_text("")
    (d / "presses").write_bytes(b"".join([
        input_event(EV_KEY, KEY_VOLUMEUP, 1),
        input_event(EV_KEY, KEY_ASSISTANT, 1), input_event(EV_SYN, 0, 0),
        input_event(EV_KEY, KEY_ASSISTANT, 0), input_event(EV_SYN, 0, 0)]))
    r = start("set:short-press='lock'", "reply:Lock=()", "state", f"feed={d}/presses", "state",
              "unplug", "state", fifo="event4")
    lines = [line for line in r.stdout.splitlines() if line.startswith(("state", "call"))]
    assert lines == [
        f"state at 0 device={d}/input/event4 cover=0 rescan=0",
        "call org.gnome.ScreenSaver /org/gnome/ScreenSaver org.gnome.ScreenSaver Lock()",
        f"state at 0 device={d}/input/event4 cover=0 rescan=0",
        # The other end closed (HUP): gone, looked for again
        "state at 0 device=- cover=0 rescan=1",
    ]


def test_service_closes_the_device_at_exit(service):
    d, start = service
    (d / "caps/event4.keys").write_text(str(KEY_ASSISTANT))
    out = start("state", "term", fifo="event4").stdout
    assert f"state at 0 device={d}/input/event4" in out
    assert out.splitlines()[-2:] == ["quit", "exit 0 fd-open=0"]
