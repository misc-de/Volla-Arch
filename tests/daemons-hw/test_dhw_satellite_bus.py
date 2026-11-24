# plinius-satellite and plinius-satellite-notify on a scripted D-Bus: main() runs with every GDBus
# call faked (nothing reaches a bus), and its main loop is replaced by the test's list of events:
# oFono appearing with modems, PropertyChanged and ModemAdded signals, NetworkManager devices,
# method calls on org.plinius.Satellite, timers firing. Checked are the calls the daemon makes
# (roaming data on and off, NetworkManager Reapply with metered), what it publishes and answers,
# and the notification the session part shows.

import os
import shutil

import pytest

from dhw_harness import PKG, build_harness, run, sandboxed_source

SAT = PKG / "plinius-satellite"

# ---- the fake bus, shared by both programs ----
FAKEBUS = r"""
#include <glib.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Replies of synchronous calls, by "METHOD PATH IFACE" (for Properties.Get "Get PATH IFACE PROP");
 * a reply text starting with ! is an error with that message */
static GHashTable *responses;
static void set_response(const char *key, const char *text)
{
	if (!responses)
		responses = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, g_free);
	g_hash_table_replace(responses, g_strdup(key), g_strdup(text));
}

static char *call_key(const char *path, const char *iface, const char *method, GVariant *args)
{
	if (!strcmp(method, "Get") && args && g_variant_is_of_type(args, G_VARIANT_TYPE("(ss)"))) {
		const char *i, *p;
		g_variant_get(args, "(&s&s)", &i, &p);
		return g_strdup_printf("Get %s %s %s", path, i, p);
	}
	return g_strdup_printf("%s %s %s", method, path, iface);
}

static GVariant *scripted(const char *key, const GVariantType *type, GError **error)
{
	const char *text = responses ? g_hash_table_lookup(responses, key) : NULL;
	GVariant *v;

	if (!text) {
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_SERVICE_UNKNOWN, "fake: nothing at %s", key);
		return NULL;
	}
	if (*text == '!') {
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_FAILED, "%s", text + 1);
		return NULL;
	}
	v = g_variant_parse(NULL, text, NULL, NULL, NULL);
	if (!v)
		abort();
	/* g_variant_parse() gives a full reference already, as GDBus does for a reply */
	/* GDBus checks the reply type */
	if (type && !g_variant_is_of_type(v, type)) {
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS, "fake: %s is no %s", text,
			    g_variant_type_peek_string(type));
		g_variant_unref(v);
		return NULL;
	}
	return v;
}

static GVariant *fake_call_sync(GDBusConnection *c, const char *dest, const char *path, const char *iface,
				const char *method, GVariant *args, const GVariantType *reply_type,
				GDBusCallFlags flags, int timeout, GCancellable *cancel, GError **error)
{
	g_autoptr(GVariant) a = args ? g_variant_ref_sink(args) : NULL;
	g_autofree char *text = a ? g_variant_print(a, TRUE) : g_strdup("");
	g_autofree char *key = call_key(path, iface, method, a);

	printf("call=%s %s %s %s%s\n", dest, path, iface, method, text);
	return scripted(key, reply_type, error);
}
#define g_dbus_connection_call_sync fake_call_sync

/* Asynchronous calls: the last one waits for the test (done|... / fail|...) */
static GAsyncReadyCallback async_cb;
static gpointer async_data;
static void fake_call(GDBusConnection *c, const char *dest, const char *path, const char *iface,
		      const char *method, GVariant *args, const GVariantType *reply_type, GDBusCallFlags flags,
		      int timeout, GCancellable *cancel, GAsyncReadyCallback cb, gpointer data)
{
	g_autoptr(GVariant) a = args ? g_variant_ref_sink(args) : NULL;
	g_autofree char *text = a ? g_variant_print(a, TRUE) : g_strdup("");

	printf("async=%s %s %s %s%s timeout=%d\n", dest, path, iface, method, text, timeout);
	if (cb) {
		async_cb = cb;
		async_data = data;
	}
}
#define g_dbus_connection_call fake_call
static GVariant *fake_call_finish(GDBusConnection *c, GAsyncResult *res, GError **error)
{
	return scripted("async", NULL, error);
}
#define g_dbus_connection_call_finish fake_call_finish

static gboolean fake_emit_signal(GDBusConnection *c, const char *dest, const char *path, const char *iface,
				 const char *name, GVariant *params, GError **error)
{
	gchar *text = g_variant_print(g_variant_ref_sink(params), TRUE);
	printf("signal=%s %s.%s%s\n", path, iface, name, text);
	g_free(text);
	g_variant_unref(params);
	return TRUE;
}
#define g_dbus_connection_emit_signal fake_emit_signal

/* Method invocations */
typedef struct { int id; const char *sender; } FakeInv;
#define g_dbus_method_invocation_get_sender(inv) (((FakeInv *)(inv))->sender)
static void fake_return_value(GDBusMethodInvocation *inv, GVariant *v)
{
	gchar *text = g_variant_print(g_variant_ref_sink(v), TRUE);
	printf("reply[%d]=%s\n", ((FakeInv *)inv)->id, text);
	g_free(text);
	g_variant_unref(v);
}
#define g_dbus_method_invocation_return_value fake_return_value
static void fake_return_gerror(GDBusMethodInvocation *inv, const GError *e)
{
	g_autofree char *name = g_dbus_error_encode_gerror(e);
	printf("error[%d]=%s: %s\n", ((FakeInv *)inv)->id, name, e->message);
}
#define g_dbus_method_invocation_return_gerror fake_return_gerror
static void fake_return_error(GDBusMethodInvocation *inv, GQuark domain, gint code, const char *fmt, ...)
{
	g_autoptr(GError) e = NULL;
	va_list ap;
	va_start(ap, fmt);
	e = g_error_new_valist(domain, code, fmt, ap);
	va_end(ap);
	fake_return_gerror(inv, e);
}
#define g_dbus_method_invocation_return_error fake_return_error

/* Timers: recorded, fired by the test */
typedef struct { guint id; guint ms; GSourceFunc fn; gpointer data; } Timer;
static GArray *timers;
static guint next_timer = 100;
static guint add_timer(guint ms, GSourceFunc fn, gpointer data)
{
	Timer t = {next_timer++, ms, fn, data};
	if (!timers)
		timers = g_array_new(FALSE, FALSE, sizeof(Timer));
	g_array_append_val(timers, t);
	printf("timer+ %ums\n", ms);
	return t.id;
}
static guint fake_timeout_add(guint ms, GSourceFunc fn, gpointer data)
{
	return add_timer(ms, fn, data);
}
static guint fake_timeout_add_seconds(guint s, GSourceFunc fn, gpointer data)
{
	return add_timer(s * 1000, fn, data);
}
static gboolean fake_source_remove(guint id)
{
	for (guint i = 0; timers && i < timers->len; i++)
		if (g_array_index(timers, Timer, i).id == id) {
			printf("timer- %ums\n", g_array_index(timers, Timer, i).ms);
			g_array_remove_index(timers, i);
			return TRUE;
		}
	abort();   /* removing a timer that is not there */
}
#define g_timeout_add fake_timeout_add
#define g_timeout_add_seconds fake_timeout_add_seconds
#define g_source_remove fake_source_remove
/* Fire the timers with this delay (0: all) that are pending now */
static void fire_timers(guint ms)
{
	GArray *due = g_array_new(FALSE, FALSE, sizeof(Timer));
	for (guint i = 0; timers && i < timers->len;) {
		Timer *t = &g_array_index(timers, Timer, i);
		if (!ms || t->ms == ms) {
			g_array_append_val(due, *t);
			g_array_remove_index(timers, i);
		} else {
			i++;
		}
	}
	for (guint j = 0; j < due->len; j++) {
		Timer *t = &g_array_index(due, Timer, j);
		printf("fire %ums\n", t->ms);
		t->fn(t->data);
	}
	g_array_free(due, TRUE);
}

/* Bus connections: plain GObjects, every call on them is faked */
static GDBusConnection *fake_bus_get_sync(GBusType type, GCancellable *c, GError **error)
{
	const char *none = getenv(type == G_BUS_TYPE_SYSTEM ? "FAKE_NO_SYSTEM_BUS" : "FAKE_NO_SESSION_BUS");
	printf("bus %s\n", type == G_BUS_TYPE_SYSTEM ? "system" : "session");
	if (none) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND, "fake: no bus");
		return NULL;
	}
	return (GDBusConnection *)g_object_new(G_TYPE_OBJECT, NULL);
}
#define g_bus_get_sync fake_bus_get_sync
static gboolean fake_flush_sync(GDBusConnection *c, GCancellable *cancel, GError **error)
{
	printf("flushed\n");
	return TRUE;
}
#define g_dbus_connection_flush_sync fake_flush_sync

/* Signal subscriptions, by member (NULL: all) */
typedef struct { char *sender, *iface, *member, *path; GDBusSignalCallback cb; } Sub;
static Sub subs[8];
static int n_subs;
static guint fake_subscribe(GDBusConnection *c, const char *sender, const char *iface, const char *member,
			    const char *path, const char *arg0, GDBusSignalFlags flags, GDBusSignalCallback cb,
			    gpointer data, GDestroyNotify free_data)
{
	printf("subscribe sender=%s iface=%s member=%s path=%s\n", sender ? sender : "*", iface ? iface : "*",
	       member ? member : "*", path ? path : "*");
	subs[n_subs] = (Sub){g_strdup(sender), g_strdup(iface), g_strdup(member), g_strdup(path), cb};
	return ++n_subs;
}
#define g_dbus_connection_signal_subscribe fake_subscribe
/* Deliver a signal to every subscription it matches, as the bus would */
static void deliver(const char *sender, const char *path, const char *iface, const char *member, const char *params)
{
	GVariant *p = g_variant_ref_sink(g_variant_parse(NULL, params, NULL, NULL, NULL));
	for (int i = 0; i < n_subs; i++)
		if ((!subs[i].sender || !strcmp(subs[i].sender, sender)) &&
		    (!subs[i].iface || !strcmp(subs[i].iface, iface)) &&
		    (!subs[i].member || !strcmp(subs[i].member, member)) &&
		    (!subs[i].path || !strcmp(subs[i].path, path)))
			subs[i].cb(NULL, ":1.7", path, iface, member, p, NULL);
	g_variant_unref(p);
}

static GSourceFunc unix_handlers[64];
static guint fake_unix_signal_add(gint signum, GSourceFunc fn, gpointer data)
{
	printf("unix-signal %d\n", signum);
	unix_handlers[signum] = fn;
	return 1;
}
#define g_unix_signal_add fake_unix_signal_add
static void fake_loop_quit(GMainLoop *loop)
{
	printf("quit\n");
}
#define g_main_loop_quit fake_loop_quit
static void run_events(void);
static void fake_loop_run(GMainLoop *loop)
{
	printf("loop\n");
	run_events();
}
#define g_main_loop_run fake_loop_run
"""

HARNESS_MAIN = r"""
static char **events;
static int n_events;
static gboolean command(char **a);

static void run_events(void)
{
	for (int i = 0; i < n_events; i++) {
		g_auto(GStrv) a = g_strsplit(events[i], "|", -1);
		if (!command(a)) {
			fprintf(stderr, "unknown event %s\n", events[i]);
			abort();
		}
	}
}

int main(int argc, char **argv)
{
	int i = 1;
	/* Commands, then "main" and the program's arguments, then "--" and the events */
	for (; i < argc && strcmp(argv[i], "main"); i++) {
		g_auto(GStrv) a = g_strsplit(argv[i], "|", -1);
		if (!command(a))
			abort();
	}
	int start = i, end = start;
	while (end < argc && strcmp(argv[end], "--"))
		end++;
	events = argv + end + 1;
	n_events = end < argc ? argc - end - 1 : 0;
	int rc = real_main(end - start, argv + start);
	printf("rc=%d\n", rc);
	return 0;
}
"""

SATELLITE = FAKEBUS + r"""
static const GDBusInterfaceVTable *vtable_registered;
static guint fake_register(GDBusConnection *c, const char *path, GDBusInterfaceInfo *info,
			   const GDBusInterfaceVTable *vt, gpointer data, GDestroyNotify free_data, GError **error)
{
	printf("register %s %s", path, info->name);
	for (GDBusMethodInfo **m = info->methods; m && *m; m++)
		printf(" method:%s", (*m)->name);
	for (GDBusPropertyInfo **p = info->properties; p && *p; p++)
		printf(" property:%s:%s", (*p)->name, (*p)->signature);
	printf("\n");
	vtable_registered = vt;
	return 1;
}
#define g_dbus_connection_register_object fake_register

static GBusNameAppearedCallback ofono_appeared;
static GBusNameVanishedCallback ofono_vanished;
static guint fake_watch(GDBusConnection *c, const char *name, GBusNameWatcherFlags flags,
			GBusNameAppearedCallback appeared, GBusNameVanishedCallback vanished, gpointer data,
			GDestroyNotify free_data)
{
	printf("watch %s\n", name);
	ofono_appeared = appeared;
	ofono_vanished = vanished;
	return 1;
}
#define g_bus_watch_name_on_connection fake_watch
static GBusNameLostCallback name_lost;
static guint fake_own(GDBusConnection *c, const char *name, GBusNameOwnerFlags flags, GBusNameAcquiredCallback acq,
		      GBusNameLostCallback lost, gpointer data, GDestroyNotify free_data)
{
	printf("own %s\n", name);
	name_lost = lost;
	return 1;
}
#define g_bus_own_name_on_connection fake_own

#define main real_main
#include "plinius-satellite.c"
#undef main
""" + HARNESS_MAIN + r"""
static gboolean command(char **a)
{
	if (!strcmp(a[0], "reply")) {
		set_response(a[1], a[2]);
	} else if (!strcmp(a[0], "ofono-up")) {
		ofono_appeared(NULL, OFONO, ":1.5", NULL);
	} else if (!strcmp(a[0], "ofono-down")) {
		ofono_vanished(NULL, OFONO, NULL);
	} else if (!strcmp(a[0], "signal")) {
		/* signal|path|iface|member|params, from oFono */
		deliver(OFONO, a[1], a[2], a[3], a[4]);
	} else if (!strcmp(a[0], "nm")) {
		/* nm|device|state */
		g_autofree char *p = g_strdup_printf("(uint32 %s, uint32 0, uint32 0)", a[2]);
		deliver(NM, a[1], NM_DEVICE, "StateChanged", p);
	} else if (!strcmp(a[0], "fire")) {
		fire_timers(a[1] ? atoi(a[1]) : 0);
	} else if (!strcmp(a[0], "method")) {
		/* method|id|sender|name|params */
		FakeInv *inv = g_new0(FakeInv, 1);
		GVariant *p = *a[4] ? g_variant_ref_sink(g_variant_parse(NULL, a[4], NULL, NULL, NULL)) : NULL;
		inv->id = atoi(a[1]);
		inv->sender = g_strdup(a[2]);
		vtable_registered->method_call(NULL, a[2], OBJECT_PATH, INTERFACE, a[3], p,
					       (GDBusMethodInvocation *)inv, NULL);
		if (p)
			g_variant_unref(p);
	} else if (!strcmp(a[0], "get")) {
		g_autoptr(GError) error = NULL;
		g_autoptr(GVariant) v = vtable_registered->get_property(NULL, ":1.9", OBJECT_PATH, INTERFACE, a[1],
									&error, NULL);
		if (v) {
			g_variant_ref_sink(v);
			g_autofree char *text = g_variant_print(v, TRUE);
			printf("property %s=%s\n", a[1], text);
		} else {
			printf("property %s error=%s\n", a[1], error->message);
		}
	} else if (!strcmp(a[0], "async-done")) {
		set_response("async", a[1]);
		async_cb(NULL, NULL, async_data);
	} else if (!strcmp(a[0], "statefile")) {
		g_autofree char *held = NULL;
		printf("statefile=%s\n", g_file_get_contents(ROAMING_STATE, &held, NULL, NULL) ? held : "-");
	} else if (!strcmp(a[0], "name-lost")) {
		name_lost(NULL, BUS_NAME, NULL);
	} else if (!strcmp(a[0], "sigterm")) {
		unix_handlers[SIGTERM](NULL);
	} else if (!strcmp(a[0], "state")) {
		printf("state active=%d modem=%s network=%s plmn=%s provider=%s held=%s metered=%s/%d leave=%d\n",
		       self.active, self.modem, self.network ? self.network : "", self.plmn ? self.plmn : "",
		       self.provider ? self.provider->id : "", self.roaming_held ? self.roaming_held : "-",
		       self.metered_device ? self.metered_device : "-", self.metered_before, self.leave_id != 0);
	} else {
		return FALSE;
	}
	return TRUE;
}
"""

NOTIFY = FAKEBUS + r"""
/* The proxy for org.plinius.Satellite: its cached properties come from the test */
static GHashTable *cached;
static GVariant *fake_cached(GDBusProxy *proxy, const char *name)
{
	GVariant *v = cached ? g_hash_table_lookup(cached, name) : NULL;
	return v ? g_variant_ref(v) : NULL;
}
#define g_dbus_proxy_get_cached_property fake_cached
static GDBusProxy *fake_proxy_new(GBusType type, GDBusProxyFlags flags, GDBusInterfaceInfo *info, const char *name,
				  const char *path, const char *iface, GCancellable *c, GError **error)
{
	printf("proxy %s %s %s %s flags=%d\n", type == G_BUS_TYPE_SYSTEM ? "system" : "session", name, path, iface,
	       flags);
	if (getenv("FAKE_NO_PROXY")) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND, "fake: no proxy");
		return NULL;
	}
	return (GDBusProxy *)g_object_new(G_TYPE_OBJECT, NULL);
}
#define g_dbus_proxy_new_for_bus_sync fake_proxy_new
static GCallback props_changed, owner_changed;
static gulong fake_signal_connect(gpointer obj, const char *signal, GCallback cb, gpointer data)
{
	printf("connect %s\n", signal);
	if (!strcmp(signal, "g-properties-changed"))
		props_changed = cb;
	else if (!strcmp(signal, "notify::g-name-owner"))
		owner_changed = cb;
	return 1;
}
#undef g_signal_connect
#define g_signal_connect(obj, signal, cb, data) fake_signal_connect(obj, signal, cb, data)

#define main real_main
#include "plinius-satellite-notify.c"
#undef main
""" + HARNESS_MAIN.replace("real_main(end - start, argv + start)", "real_main()") + r"""
static gboolean command(char **a)
{
	if (!strcmp(a[0], "reply")) {
		set_response(a[1], a[2]);
	} else if (!strcmp(a[0], "prop")) {
		/* prop|name|value, "-" removes it */
		if (!cached)
			cached = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, (GDestroyNotify)g_variant_unref);
		if (!strcmp(a[2], "-"))
			g_hash_table_remove(cached, a[1]);
		else
			g_hash_table_replace(cached, g_strdup(a[1]),
					     g_variant_ref_sink(g_variant_parse(NULL, a[2], NULL, NULL, NULL)));
	} else if (!strcmp(a[0], "changed")) {
		((void (*)(GDBusProxy *, GVariant *, const char *const *, gpointer))props_changed)(self.sat, NULL, NULL,
												  NULL);
	} else if (!strcmp(a[0], "owner")) {
		((void (*)(GObject *, GParamSpec *, gpointer))owner_changed)(NULL, NULL, NULL);
	} else if (!strcmp(a[0], "closed")) {
		/* closed|id|member */
		g_autofree char *p = g_strdup_printf("(uint32 %s, uint32 2)", a[1]);
		deliver(NOTIFY_NAME, NOTIFY_PATH, NOTIFY_INTERFACE, a[2], p);
	} else if (!strcmp(a[0], "sigterm")) {
		unix_handlers[SIGTERM](NULL);
	} else if (!strcmp(a[0], "state")) {
		printf("state note=%u shown_for=%s\n", self.note_id, self.shown_for ? self.shown_for : "-");
	} else {
		return FALSE;
	}
	return TRUE;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("satellite_bus")
    sandboxed_source(SAT / "plinius-satellite.c", d, [
        ("/usr/share/plinius-satellite/providers.conf", d / "providers.conf"),
        ("/etc/plinius-satellite/providers.d", d / "providers.d"),
        ("/etc/plinius-satellite/satellite.conf", d / "satellite.conf"),
        ("/var/lib/plinius-satellite/roaming", d / "state" / "roaming"),
    ])
    sandboxed_source(SAT / "plinius-satellite-notify.c", d, [])
    sat = build_harness(d, "satellite_bus", SATELLITE, pkgs=("gio-unix-2.0",))
    notify = build_harness(d, "satellite_notify", NOTIFY, pkgs=("gio-unix-2.0",))
    return d, sat, notify


@pytest.fixture
def sat(built):
    d, exe, _ = built
    shutil.copy(SAT / "providers.conf", d / "providers.conf")
    shutil.rmtree(d / "providers.d", ignore_errors=True)
    (d / "providers.d").mkdir()
    (d / "satellite.conf").unlink(missing_ok=True)
    shutil.rmtree(d / "state", ignore_errors=True)
    (d / "state").mkdir()

    def go(*events, pre=(), args=(), env=None, check=True):
        e = dict(os.environ)
        e.update(env or {})
        return run(exe, *pre, "main", *args, "--", *events, env=e, check=check)
    go.dir = d
    return go


NETREG = "org.ofono.NetworkRegistration"
CONNMAN = "org.ofono.ConnectionManager"
DEV = "org.freedesktop.NetworkManager.Device"


def modems(*entries):
    """GetModems reply: (path, interfaces) pairs."""
    items = ", ".join(f"('{p}', {{'Interfaces': <{list(i)}>}})" for p, i in entries)
    return f"reply|GetModems / org.ofono.Manager|(@a(oa{{sv}}) [{items}],)"


def netreg(path, status, mcc, mnc, name):
    return (f"reply|GetProperties {path} {NETREG}|({{'Status': <'{status}'>, 'MobileCountryCode': <'{mcc}'>, "
            f"'MobileNetworkCode': <'{mnc}'>, 'Name': <'{name}'>, 'Strength': <byte 50>}},)")


def connman(path, roaming):
    return f"reply|GetProperties {path} {CONNMAN}|({{'RoamingAllowed': <{str(roaming).lower()}>, 'Attached': <true>}},)"


SET_PROPERTY_OK = [f"reply|SetProperty /ril_0 {CONNMAN}|()"]
ON_SATELLITE = [modems(("/ril_0", [NETREG, CONNMAN])), netreg("/ril_0", "roaming", "310", "830", "T-Mobile SpaceX"),
                connman("/ril_0", False), *SET_PROPERTY_OK]
ON_TOWER = [modems(("/ril_0", [NETREG, CONNMAN])), netreg("/ril_0", "registered", "262", "01", "Telekom.de"),
            connman("/ril_0", False), *SET_PROPERTY_OK]


def lines(out, prefix):
    return [line[len(prefix):] for line in out.splitlines() if line.startswith(prefix)]


def states(out):
    return lines(out, "state ")


def roaming_calls(out):
    return [line for line in out.splitlines() if "SetProperty" in line]


# ---- main() ----

def test_main_wiring_and_clean_exit(sat):
    out = sat("state", "sigterm").stdout
    assert ("register /org/plinius/Satellite org.plinius.Satellite method:Scan method:Assess "
            "property:Active:b property:Modem:o property:Network:s property:Plmn:s property:Provider:s "
            "property:Services:as") in out
    subscribes = lines(out, "subscribe ")
    assert subscribes == ["sender=org.ofono iface=* member=* path=*",
                          "sender=org.freedesktop.NetworkManager iface=org.freedesktop.NetworkManager.Device "
                          "member=StateChanged path=*"]
    assert "watch org.ofono" in out and "own org.plinius.Satellite" in out
    assert "unix-signal 15" in out and "unix-signal 2" in out
    assert states(out)[0] == "active=0 modem=/ network= plmn= provider= held=- metered=-/0 leave=0"
    # SIGTERM ends the loop; off the satellite there is nothing to publish or undo
    tail = out.split("loop\n", 1)[1].splitlines()
    assert tail[1:] == ["quit", "flushed", "rc=0"]


def test_exit_on_the_satellite_undoes_everything(sat):
    out = sat("ofono-up", "statefile", "sigterm", pre=ON_SATELLITE + NM_DEVICES).stdout
    assert "statefile=/ril_0" in out
    tail = out.split("quit\n", 1)[1].splitlines()
    # Published as off, roaming data off, metered back, all before the bus is flushed
    assert tail[0].startswith("signal=/org/plinius/Satellite org.freedesktop.DBus.Properties.PropertiesChanged"
                              "('org.plinius.Satellite', {'Active': <false>")
    assert tail[1] == f"call=org.ofono /ril_0 {CONNMAN} SetProperty('RoamingAllowed', <false>)"
    assert [line for line in tail if " Reapply(" in line][0].startswith(
        f"call=org.freedesktop.NetworkManager /nm/2 {DEV} Reapply({{'connection': {{'id': <'mobile'>, 'metered': <2>}}")
    assert tail[-2:] == ["flushed", "rc=0"]
    assert not (sat.dir / "state/roaming").exists()


def test_main_usage(sat):
    r = sat(args=("--bogus",))
    assert "rc=2" in r.stdout
    assert "Usage:" in r.stderr and "[--scan]" in r.stderr


def test_main_without_system_bus(sat):
    r = sat(env={"FAKE_NO_SYSTEM_BUS": "1"})
    assert "rc=1" in r.stdout
    assert "No system bus: fake: no bus" in r.stderr


def test_name_lost_ends_the_loop(sat):
    r = sat("name-lost")
    assert "quit" in r.stdout.split("loop\n", 1)[1]
    assert "Lost the bus name org.plinius.Satellite" in r.stderr


def test_broken_providers_file_is_reported(sat):
    (sat.dir / "providers.d/30-broken.conf").write_text("this is [not a key file\n")
    (sat.dir / "providers.d/40-ok.conf").write_text("[Mine]\nPlmn=99901\n")
    r = sat()
    assert "30-broken.conf:" in r.stderr
    # The shipped list plus the one entry of the file that could be read
    shipped = sum(1 for line in (SAT / "providers.conf").read_text().splitlines() if line.startswith("["))
    assert f"{shipped + 1} satellite providers known" in r.stderr


# ---- oFono and roaming data ----

def test_ofono_appears_with_a_modem_on_the_satellite(sat):
    out = sat("ofono-up", "state", "statefile", "get|Active", "get|Modem", "get|Network", "get|Plmn", "get|Provider",
              "get|Services", "get|Bogus", pre=ON_SATELLITE).stdout
    calls = lines(out, "call=")
    assert calls[:3] == ["org.ofono / org.ofono.Manager GetModems",
                         f"org.ofono /ril_0 {NETREG} GetProperties",
                         f"org.ofono /ril_0 {CONNMAN} GetProperties"]
    assert states(out)[0] == ("active=1 modem=/ril_0 network=T-Mobile SpaceX plmn=310830 provider=T-Mobile US "
                              "held=/ril_0 metered=-/0 leave=0")
    # On, and off again when the program ends
    assert roaming_calls(out) == [
        f"call=org.ofono /ril_0 {CONNMAN} SetProperty('RoamingAllowed', <true>)",
        f"call=org.ofono /ril_0 {CONNMAN} SetProperty('RoamingAllowed', <false>)"]
    assert "statefile=/ril_0" in out
    assert lines(out, "property ") == [
        "Active=true", "Modem=objectpath '/ril_0'", "Network='T-Mobile SpaceX'", "Plmn='310830'",
        "Provider='T-Mobile T-Satellite'", "Services=['sms', 'mms', 'data']", "Bogus error=No property Bogus"]


def test_properties_off_the_satellite(sat):
    out = sat("get|Network", "get|Plmn", "get|Provider", "get|Services").stdout
    assert lines(out, "property ") == ["Network=''", "Plmn=''", "Provider=''", "Services=@as []"]


def test_roaming_left_on_by_an_earlier_run_is_undone(sat):
    (sat.dir / "state/roaming").write_text("/ril_0")
    out = sat("ofono-up", "state", pre=ON_TOWER).stdout
    assert roaming_calls(out) == [
        f"call=org.ofono /ril_0 {CONNMAN} SetProperty('RoamingAllowed', <false>)"]
    assert not (sat.dir / "state/roaming").exists()
    assert states(out)[0].endswith("held=- metered=-/0 leave=0")


def test_state_file_that_is_no_object_path_is_ignored(sat):
    (sat.dir / "state/roaming").write_text("not a path")
    out = sat("ofono-up", "state", pre=ON_TOWER).stdout
    assert roaming_calls(out) == []
    assert states(out)[0].endswith("held=- metered=-/0 leave=0")


def test_roaming_is_not_switched_on_without_a_state_file(sat):
    shutil.rmtree(sat.dir / "state")
    r = sat("ofono-up", "state", pre=ON_SATELLITE)
    # No way to remember it: roaming data stays off rather than staying on after a crash
    assert roaming_calls(r.stdout) == []
    assert "Not allowing roaming data, no state file" in r.stderr
    assert states(r.stdout)[0].startswith("active=1 ")


def test_roaming_refused_by_ofono_leaves_no_state_file(sat):
    pre = ON_SATELLITE[:-1] + [f"reply|SetProperty /ril_0 {CONNMAN}|!org.ofono.Error.Failed"]
    r = sat("ofono-up", "state", pre=pre)
    assert len(roaming_calls(r.stdout)) == 1
    assert "cannot allow roaming data: org.ofono.Error.Failed" in r.stderr
    assert not (sat.dir / "state/roaming").exists()
    assert "held=-" in states(r.stdout)[0]


def test_roaming_already_allowed_by_the_user_is_left_alone(sat):
    pre = ON_SATELLITE[:2] + [connman("/ril_0", True)]
    out = sat("ofono-up", "state", pre=pre).stdout
    assert roaming_calls(out) == []
    assert "held=-" in states(out)[0]


def test_undoing_roaming_that_fails_keeps_the_state_file(sat):
    out = sat("ofono-up", f"reply|SetProperty /ril_0 {CONNMAN}|!busy",
              "signal|/ril_0|org.ofono.NetworkRegistration|PropertyChanged|('Status', <'registered'>)",
              "signal|/ril_0|org.ofono.NetworkRegistration|PropertyChanged|('MobileCountryCode', <'262'>)",
              "signal|/ril_0|org.ofono.NetworkRegistration|PropertyChanged|('Name', <'Telekom.de'>)",
              "fire|500", "state", pre=ON_SATELLITE).stdout
    # Tried when leaving and again at the exit; the file still says what to undo
    assert [c.rsplit("'RoamingAllowed', ", 1)[1] for c in roaming_calls(out)] == ["<true>)", "<false>)", "<false>)"]
    assert states(out)[-1].startswith("active=0 ") and "held=/ril_0" in states(out)[-1]
    assert (sat.dir / "state/roaming").read_text() == "/ril_0"


def test_ofono_vanishing_forgets_everything(sat):
    out = sat("ofono-up", "ofono-down", "state", "ofono-down", "sigterm", pre=ON_SATELLITE).stdout
    assert states(out)[0] == "active=0 modem=/ network= plmn= provider= held=- metered=-/0 leave=0"
    # Roaming is not undone now (no oFono), the state file does it when oFono is back
    assert len(roaming_calls(out)) == 1
    assert (sat.dir / "state/roaming").read_text() == "/ril_0"
    # A second vanish (oFono was never up again) does nothing
    assert out.count("'Active': <false>") == 1


def test_no_modems_when_ofono_does_not_answer(sat):
    out = sat("ofono-up", "state").stdout
    assert states(out)[0].startswith("active=0 modem=/ ")


# ---- signals ----

def sig(path, iface, member, params):
    return f"signal|{path}|{iface}|{member}|{params}"


def test_property_changes_settle_before_evaluating(sat):
    out = sat("ofono-up",
              sig("/ril_0", NETREG, "PropertyChanged", "('Status', <'roaming'>)"),
              sig("/ril_0", NETREG, "PropertyChanged", "('MobileCountryCode', <'310'>)"),
              sig("/ril_0", NETREG, "PropertyChanged", "('MobileNetworkCode', <'830'>)"),
              sig("/ril_0", NETREG, "PropertyChanged", "('Name', <'T-Mobile SpaceX'>)"),
              sig("/ril_0", NETREG, "PropertyChanged", "('Strength', <byte 40>)"),
              sig("/ril_0", NETREG, "PropertyChanged", "('Status', <uint32 1>)"),
              "state", "fire|500", "state", pre=ON_TOWER).stdout
    # One timer for the whole burst of changes
    assert lines(out, "timer+ ") == ["500ms"]
    s = states(out)
    assert s[0].startswith("active=0 ")
    assert s[1].startswith("active=1 modem=/ril_0 network=T-Mobile SpaceX plmn=310830 ")


def test_signals_of_unknown_modems_and_other_signals_are_ignored(sat):
    out = sat("ofono-up",
              sig("/ril_9", NETREG, "PropertyChanged", "('Status', <'roaming'>)"),
              sig("/ril_0", NETREG, "OperatorsChanged", "(@a(oa{sv}) [],)"),
              "state", pre=ON_TOWER).stdout
    assert lines(out, "timer+ ") == []


def test_modem_added_and_removed(sat):
    added = ("(objectpath '/ril_1', {'Interfaces': <['org.ofono.NetworkRegistration', "
             "'org.ofono.ConnectionManager']>})")
    out = sat("ofono-up", netreg("/ril_1", "roaming", "310", "830", ""), connman("/ril_1", True),
              sig("/", "org.ofono.Manager", "ModemAdded", added), "fire|500", "state",
              sig("/", "org.ofono.Manager", "ModemRemoved", "(objectpath '/ril_1',)"), "fire|500", "state",
              sig("/", "org.ofono.Manager", "Other", "()"), "fire|500", "fire|60000", "state",
              pre=[modems()]).stdout
    s = states(out)
    assert s[0].startswith("active=1 modem=/ril_1 network= plmn=310830 ")
    # Gone: still on the satellite for the hysteresis, then off
    assert s[1].startswith("active=1 modem=/ril_1 ") and s[1].endswith("leave=1")
    assert s[2].startswith("active=0 ")


def test_interfaces_going_and_coming(sat):
    pre = [modems(("/ril_0", ["org.ofono.SimManager"])),
           netreg("/ril_0", "roaming", "310", "830", "T-Mobile SpaceX"), connman("/ril_0", False),
           *SET_PROPERTY_OK]
    both = "('Interfaces', <['org.ofono.NetworkRegistration', 'org.ofono.ConnectionManager']>)"
    out = sat("ofono-up", "state",
              sig("/ril_0", "org.ofono.Modem", "PropertyChanged", both), "fire|500", "state",
              # Same interfaces again: nothing is read twice
              sig("/ril_0", "org.ofono.Modem", "PropertyChanged", both), "fire|500",
              sig("/ril_0", "org.ofono.Modem", "PropertyChanged", "('Powered', <true>)"), "fire|500",
              # Registration gone: its values with it, the satellite is lost (hysteresis)
              sig("/ril_0", "org.ofono.Modem", "PropertyChanged", "('Interfaces', <@as []>)"), "fire|500",
              "state", pre=pre).stdout
    s = states(out)
    assert s[0].startswith("active=0 ")
    assert s[1].startswith("active=1 modem=/ril_0 ") and "held=/ril_0" in s[1]
    reads = [c for c in lines(out, "call=") if "GetProperties" in c]
    assert reads == [f"org.ofono /ril_0 {NETREG} GetProperties", f"org.ofono /ril_0 {CONNMAN} GetProperties"]
    assert s[2].startswith("active=1 ") and s[2].endswith("leave=1")
    # Without the ConnectionManager there is nothing to switch off: left to the state file, also at exit
    assert len(roaming_calls(out)) == 1
    assert (sat.dir / "state/roaming").read_text() == "/ril_0"


def test_user_switches_roaming_off_by_signal(sat):
    out = sat("ofono-up", sig("/ril_0", CONNMAN, "PropertyChanged", "('RoamingAllowed', <false>)"),
              sig("/ril_0", CONNMAN, "PropertyChanged", "('Attached', <true>)"), "state",
              pre=ON_SATELLITE).stdout
    assert "held=-" in states(out)[0]
    assert not (sat.dir / "state/roaming").exists()


SATELLITE_BY_PLMN = [ON_SATELLITE[0], netreg("/ril_0", "roaming", "310", "830", ""), *ON_SATELLITE[2:]]


@pytest.mark.parametrize("iface", ["org.ofono.SimManager", "org.ofono.NetworkOperator", "org.ofono.VoiceCallManager"])
def test_sim_manager_changes_do_not_replace_the_network(sat, iface):
    out = sat("ofono-up",
              sig("/ril_0", iface, "PropertyChanged", "('MobileCountryCode', <'262'>)"),
              sig("/ril_0", iface, "PropertyChanged", "('MobileNetworkCode', <'01'>)"),
              sig("/ril_0", iface, "PropertyChanged", "('Name', <'Telekom.de'>)"),
              sig("/ril_0", iface, "PropertyChanged", "('Status', <'registered'>)"),
              "fire|500", "state", pre=SATELLITE_BY_PLMN).stdout
    # Still the satellite's network, found by its PLMN alone (and the timer for leaving not started)
    assert states(out)[-1] == ("active=1 modem=/ril_0 network= plmn=310830 provider=T-Mobile US held=/ril_0 "
                               "metered=-/0 leave=0")


# ---- hysteresis ----

def test_satellite_found_again_while_waiting(sat):
    out = sat("ofono-up",
              sig("/ril_0", NETREG, "PropertyChanged", "('Status', <'searching'>)"), "fire|500", "state",
              sig("/ril_0", NETREG, "PropertyChanged", "('Status', <'roaming'>)"), "fire|500", "state",
              pre=ON_SATELLITE).stdout
    s = states(out)
    assert s[0].endswith("leave=1") and s[1].endswith("leave=0")
    assert lines(out, "timer+ ") == ["500ms", "60000ms", "500ms"]
    assert lines(out, "timer- ") == ["60000ms"]
    assert s[1].startswith("active=1 ")


def test_back_on_a_tower_while_waiting_leaves_at_once(sat):
    (sat.dir / "satellite.conf").write_text("[Policy]\nHysteresis=5\n")
    out = sat("ofono-up",
              sig("/ril_0", NETREG, "PropertyChanged", "('Status', <'searching'>)"), "fire|500",
              sig("/ril_0", NETREG, "PropertyChanged", "('Status', <'registered'>)"),
              sig("/ril_0", NETREG, "PropertyChanged", "('MobileCountryCode', <'262'>)"),
              sig("/ril_0", NETREG, "PropertyChanged", "('Name', <'Telekom.de'>)"), "fire|500", "state",
              pre=ON_SATELLITE).stdout
    assert lines(out, "timer+ ")[1] == "5000ms"
    assert "5000ms" in lines(out, "timer- ")
    assert states(out)[-1].startswith("active=0 ") and states(out)[-1].endswith("leave=0")


def test_hysteresis_runs_out(sat):
    out = sat("ofono-up", sig("/ril_0", NETREG, "PropertyChanged", "('Status', <'searching'>)"), "fire|500",
              "fire|60000", "state", pre=ON_SATELLITE).stdout
    assert "fire 60000ms" in out
    assert states(out)[-1].startswith("active=0 ") and states(out)[-1].endswith("held=- metered=-/0 leave=0")


# ---- metered ----

NM_DEVICES = [
    "reply|GetDevices /org/freedesktop/NetworkManager org.freedesktop.NetworkManager|(@ao ['/nm/1', '/nm/2'],)",
    f"reply|Get /nm/1 {DEV} Interface|(<'wlan0'>,)",
    f"reply|Get /nm/2 {DEV} Interface|(<'/ril_0'>,)",
    f"reply|Get /nm/2 {DEV} State|(<uint32 100>,)",
    f"reply|GetAppliedConnection /nm/2 {DEV}|({{'connection': {{'id': <'mobile'>, 'metered': <2>}}, "
    f"'gsm': {{'apn': <'internet'>}}}}, uint64 7)",
    f"reply|Reapply /nm/2 {DEV}|()",
]


def reapplies(out):
    return [c.split(" Reapply", 1)[1] for c in lines(out, "call=") if " Reapply" in c]


def test_mobile_connection_is_metered_on_the_satellite(sat):
    out = sat("ofono-up", "state",
              sig("/ril_0", NETREG, "PropertyChanged", "('Status', <'registered'>)"),
              sig("/ril_0", NETREG, "PropertyChanged", "('MobileCountryCode', <'262'>)"),
              sig("/ril_0", NETREG, "PropertyChanged", "('Name', <'Telekom.de'>)"), "fire|500", "state",
              pre=ON_SATELLITE + NM_DEVICES).stdout
    r = reapplies(out)
    # Only connection.metered changes, the other settings and the version go back unchanged
    assert r[0] == ("({'connection': {'id': <'mobile'>, 'metered': <1>}, 'gsm': {'apn': <'internet'>}}, "
                    "uint64 7, uint32 0)")
    assert states(out)[0].endswith("metered=/nm/2/2 leave=0")
    # Off the satellite: the value from before, 2 (guess-no); nothing more to undo at exit
    assert len(r) == 2
    assert r[1].startswith("({'connection': {'id': <'mobile'>, 'metered': <2>}")
    assert states(out)[1].endswith("metered=-/2 leave=0")


def test_policy_without_metered(sat):
    (sat.dir / "satellite.conf").write_text("[Policy]\nMetered=false\n")
    out = sat("ofono-up", "state", pre=ON_SATELLITE + NM_DEVICES).stdout
    assert reapplies(out) == []


@pytest.mark.parametrize("change,expected_warning", [
    (f"reply|Get /nm/2 {DEV} State|(<uint32 30>,)", None),                  # not activated (yet)
    (f"reply|Get /nm/2 {DEV} State|!gone", None),
    (f"reply|GetAppliedConnection /nm/2 {DEV}|!no connection", "/nm/2: no applied connection: no connection"),
    (f"reply|Reapply /nm/2 {DEV}|!version changed", "/nm/2: cannot set metered to 1: version changed"),
    ("reply|GetDevices /org/freedesktop/NetworkManager org.freedesktop.NetworkManager|!no NM", None),
    (f"reply|Get /nm/2 {DEV} Interface|(<'/ril_1'>,)", None),               # no device of this modem
])
def test_metered_not_possible(sat, change, expected_warning):
    r = sat("ofono-up", "state", pre=ON_SATELLITE + NM_DEVICES + [change])
    assert states(r.stdout)[0].endswith("metered=-/0 leave=0")
    if expected_warning:
        assert expected_warning in r.stderr


def test_mobile_connection_coming_up_later(sat):
    out = sat(f"reply|Get /nm/2 {DEV} State|(<uint32 30>,)", "ofono-up", "state",
              f"reply|Get /nm/2 {DEV} State|(<uint32 100>,)", "nm|/nm/1|50", "state",
              "nm|/nm/2|100", "state", pre=ON_SATELLITE + NM_DEVICES).stdout
    s = states(out)
    assert s[0].endswith("metered=-/0 leave=0")
    assert s[1].endswith("metered=-/0 leave=0")       # another device, not activated
    assert s[2].endswith("metered=/nm/2/2 leave=0")


def test_mobile_connection_going_down_takes_the_change_with_it(sat):
    out = sat("ofono-up", "nm|/nm/2|110", "state", "ofono-down", pre=ON_SATELLITE + NM_DEVICES).stdout
    assert states(out)[0].endswith("metered=-/2 leave=0")
    # Nothing left to undo when oFono goes
    assert len(reapplies(out)) == 1


def test_device_gone_when_undoing(sat):
    r = sat("ofono-up", f"reply|Get /nm/2 {DEV} State|(<uint32 20>,)", "ofono-down", "state",
            pre=ON_SATELLITE + NM_DEVICES)
    assert len(reapplies(r.stdout)) == 1
    assert states(r.stdout)[0].endswith("metered=-/2 leave=0")
    assert "metered back to" not in r.stderr


def test_device_changed_while_on_the_satellite(sat):
    # oFono2MM recreated the device: the old one is set back, the new one made metered
    out = sat("ofono-up",
              "reply|GetDevices /org/freedesktop/NetworkManager org.freedesktop.NetworkManager|(@ao ['/nm/3'],)",
              f"reply|Get /nm/3 {DEV} Interface|(<'/ril_0'>,)", f"reply|Get /nm/3 {DEV} State|(<uint32 100>,)",
              f"reply|GetAppliedConnection /nm/3 {DEV}|({{'connection': {{'id': <'m'>}}}}, uint64 1)",
              f"reply|Reapply /nm/3 {DEV}|()", "nm|/nm/3|100", "state", pre=ON_SATELLITE + NM_DEVICES).stdout
    r = [c for c in lines(out, "call=") if " Reapply" in c]
    # The last one is the exit
    assert [c.split()[1] for c in r] == ["/nm/2", "/nm/2", "/nm/3", "/nm/3"]
    assert states(out)[0].endswith("metered=/nm/3/0 leave=0")


# ---- Scan and Assess ----

SESSION = [
    "reply|Get /org/freedesktop/login1/seat/seat0 org.freedesktop.login1.Seat ActiveSession|"
    "(<('c1', objectpath '/org/freedesktop/login1/session/c1')>,)",
    "reply|Get /org/freedesktop/login1/session/c1 org.freedesktop.login1.Session User|"
    "(<(uint32 1000, objectpath '/org/freedesktop/login1/user/_1000')>,)",
    "reply|Get /org/freedesktop/login1/session/c1 org.freedesktop.login1.Session LockedHint|(<false>,)",
]
SIM = [f"reply|GetProperties /ril_0 org.ofono.SimManager|({{'MobileCountryCode': <'262'>, "
       f"'MobileNetworkCode': <'01'>}},)"]


def uid(n):
    return f"reply|GetConnectionUnixUser /org/freedesktop/DBus org.freedesktop.DBus|(uint32 {n},)"


SCANNED = ("(@a(oa{sv}) [('/ril_0/operator/26201', {'MobileCountryCode': <'262'>, 'MobileNetworkCode': <'01'>, "
           "'Name': <'Telekom.de'>, 'Technologies': <['lte']>}), "
           "('/ril_0/operator/26203', {'MobileCountryCode': <'262'>, 'MobileNetworkCode': <'03'>, "
           "'Name': <'O2.de'>}), "
           "('/ril_0/operator/x', {'Name': <'T-Mobile SpaceX'>})],)")


def test_scan_answers_every_waiting_caller(sat):
    out = sat("ofono-up", uid(0), "method|1|:1.20|Scan|", "method|2|:1.21|Scan|", f"async-done|{SCANNED}",
              pre=ON_TOWER + SIM).stdout
    assert lines(out, "async=") == [f"org.ofono /ril_0 {NETREG} Scan timeout=240000"]
    replies = lines(out, "reply[")
    assert len(replies) == 2 and replies[0][1:] == replies[1][1:]
    r = replies[0]
    assert ("{'Plmn': <'26201'>, 'Name': <'Telekom.de'>, 'Technologies': <['lte']>, 'Kind': <'home'>, "
            "'Verdict': <'registered'>, 'Provider': <''>}") in r
    assert "{'Plmn': <'26203'>, 'Name': <'O2.de'>, 'Technologies': <@as []>, 'Kind': <'national'>, " \
           "'Verdict': <'unlikely'>, 'Provider': <''>}" in r
    assert "{'Plmn': <''>, 'Name': <'T-Mobile SpaceX'>, 'Technologies': <@as []>, 'Kind': <'satellite'>, " \
           "'Verdict': <'maybe'>, 'Provider': <'T-Mobile T-Satellite'>, 'Services': <['sms'" in r


def test_scan_answer_is_not_leaked_per_caller(sat):
    # The reply is built once for every waiting caller; each return takes a reference of its own
    env = {"ASAN_OPTIONS": "detect_leaks=1:exitcode=86", "LSAN_OPTIONS": "exitcode=0"}
    import subprocess
    d = sat.dir
    e = dict(os.environ, G_DEBUG="", LC_ALL="C", **env)
    r = subprocess.run([str(d / "satellite_bus"), *ON_TOWER, *SIM, "main", "--", "ofono-up", uid(0),
                        "method|1|:1.20|Scan|", "method|2|:1.21|Scan|", "method|3|:1.22|Scan|",
                        f"async-done|{SCANNED}"], capture_output=True, text=True, env=e, timeout=60)
    assert r.returncode == 0, r.stderr
    assert len(lines(r.stdout, "reply[")) == 3
    leaks = [block for block in r.stderr.split("\n\n") if "leak of" in block and "plinius-satellite.c" in block]
    assert not leaks, leaks


def test_scan_failure_reaches_every_caller(sat):
    r = sat("ofono-up", uid(0), "method|1|:1.20|Scan|", "method|2|:1.21|Scan|", "async-done|!scan aborted",
            # A new scan after the failed one starts again
            "method|3|:1.20|Scan|", pre=ON_TOWER)
    errors = lines(r.stdout, "error[")
    assert errors == ["1]=org.freedesktop.DBus.Error.Failed: scan aborted",
                      "2]=org.freedesktop.DBus.Error.Failed: scan aborted"]
    assert len(lines(r.stdout, "async=")) == 2
    assert "/ril_0: scan failed: scan aborted" in r.stderr


def test_scan_without_sim_data_and_off_registration(sat):
    pre = [modems(("/ril_0", [NETREG])), netreg("/ril_0", "searching", "", "", "")]
    out = sat("ofono-up", uid(0), "method|1|:1.20|Scan|", f"async-done|{SCANNED}", pre=pre).stdout
    r = lines(out, "reply[")[0]
    # Unknown SIM, not registered: everything is roaming, maybe
    assert r.count("'Kind': <'roaming'>") == 2 and "'Verdict': <'registered'>" not in r


def test_scan_uses_the_satellite_modem(sat):
    pre = [modems(("/ril_0", [NETREG]), ("/ril_1", [NETREG, CONNMAN])),
           netreg("/ril_0", "registered", "262", "01", "Telekom.de"),
           netreg("/ril_1", "roaming", "310", "830", ""), connman("/ril_1", True)]
    out = sat("ofono-up", uid(0), "method|1|:1.20|Scan|", pre=pre).stdout
    assert lines(out, "async=") == [f"org.ofono /ril_1 {NETREG} Scan timeout=240000"]


def test_scan_without_a_modem(sat):
    out = sat("ofono-up", uid(0), "method|1|:1.20|Scan|", pre=[modems(("/ril_0", ["org.ofono.SimManager"]))]).stdout
    assert lines(out, "error[") == ["1]=org.freedesktop.DBus.Error.Failed: No modem with a network registration"]
    assert lines(out, "async=") == []


@pytest.mark.parametrize("script,allowed", [
    ([uid(1000)] + SESSION, True),                                   # the user at the unlocked phone
    ([uid(1001)] + SESSION, False),                                  # someone else
    ([uid(1000)] + SESSION[:2] + [SESSION[2].replace("false", "true")], False),   # locked
    ([uid(1000)] + SESSION[:2] + [SESSION[2].replace("<false>", "<'no'>")], False),  # wrong type
    ([uid(1000)] + SESSION[1:], False),                              # no active session
    ([uid(1000), SESSION[0].replace("<('c1', objectpath '/org/freedesktop/login1/session/c1')>", "<'c1'>")]
     + SESSION[1:], False),
    ([uid(1000), SESSION[0], SESSION[2]], False),                    # no user of the session
])
def test_who_may_scan(sat, script, allowed):
    out = sat("ofono-up", *script, "method|1|:1.20|Scan|", pre=ON_TOWER).stdout
    if allowed:
        assert len(lines(out, "async=")) == 1 and lines(out, "error[") == []
    else:
        assert lines(out, "error[") == [
            "1]=org.freedesktop.DBus.Error.AccessDenied: Only the user of the unlocked phone may scan for networks"]
        assert lines(out, "async=") == []


def test_scan_caller_unknown_to_the_bus(sat):
    out = sat("ofono-up", "method|1|:1.20|Scan|", pre=ON_TOWER).stdout
    assert lines(out, "error[")[0].startswith("1]=org.freedesktop.DBus.Error.ServiceUnknown: fake: nothing at "
                                              "GetConnectionUnixUser")


def test_assess(sat):
    out = sat("ofono-up", "method|1|:1.20|Assess|([('26201', 'Telekom.de'), ('23410', 'O2 UK')],)",
              pre=ON_TOWER + SIM).stdout
    r = lines(out, "reply[")[0]
    assert "'Plmn': <'26201'>, 'Name': <'Telekom.de'>, 'Technologies': <@as []>, 'Kind': <'home'>, " \
           "'Verdict': <'registered'>" in r
    assert "'Plmn': <'23410'>, 'Name': <'O2 UK'>, 'Technologies': <@as []>, 'Kind': <'roaming'>, " \
           "'Verdict': <'maybe'>" in r


def test_assess_with_sim_without_network_code(sat):
    sim = f"reply|GetProperties /ril_0 org.ofono.SimManager|({{'MobileCountryCode': <'262'>}},)"
    out = sat("ofono-up", "method|1|:1.20|Assess|([('26201', 'Telekom.de')],)", pre=ON_TOWER[:1] + [sim]).stdout
    # The SIM's network unknown, the modem's registration unreadable: no home, nothing registered
    assert "'Kind': <'roaming'>, 'Verdict': <'maybe'>" in lines(out, "reply[")[0]
    assert states(sat("ofono-up", "state", pre=ON_TOWER[:1]).stdout)[0].startswith("active=0 modem=/ ")


def test_assess_without_modems(sat):
    out = sat("ofono-up", "method|1|:1.20|Assess|([('26201', '')],)").stdout
    assert "'Kind': <'roaming'>, 'Verdict': <'maybe'>" in lines(out, "reply[")[0]


def test_unknown_method(sat):
    out = sat("method|1|:1.20|Reboot|").stdout
    assert lines(out, "error[") == ["1]=org.freedesktop.DBus.Error.UnknownMethod: No method Reboot"]


# ---- plinius-satellite --scan ----

SCAN_KEY = "reply|Scan /org/plinius/Satellite org.plinius.Satellite"


def test_scan_cli_prints_the_table(sat):
    nets = ("(@aa{sv} [{'Plmn': <'26201'>, 'Name': <'Telekom.de'>, 'Technologies': <['gsm', 'lte']>, "
            "'Kind': <'home'>, 'Verdict': <'registered'>, 'Provider': <''>}, "
            "{'Plmn': <'26201'>, 'Name': <'Telekom.de'>, 'Kind': <'home'>, 'Verdict': <'yes'>}, "
            "{'Plmn': <'310830'>, 'Name': <'T-Mobile SpaceX with a much too long name'>, 'Kind': <'satellite'>, "
            "'Verdict': <'maybe'>, 'Provider': <'T-Mobile T-Satellite'>}, "
            "{'Plmn': <'26203'>, 'Kind': <'national'>, 'Verdict': <'unlikely'>}, "
            "{'Plmn': <'23410'>, 'Kind': <'roaming'>, 'Verdict': <'maybe'>}],)")
    r = sat(pre=[f"{SCAN_KEY}|{nets}"], args=("--scan",))
    out = r.stdout.splitlines()
    assert "Scanning, this takes about a minute..." in out
    i = out.index("PLMN    NAME                       TECH         KIND       REGISTER?   NOTE")
    assert out[i + 1:i + 6] == [
        "26201   Telekom.de                 gsm,lte      home       registered  on it now",
        "26201   Telekom.de                              home       yes         network of the SIM",
        "310830  T-Mobile SpaceX with a muc              satellite  maybe       satellite, the provider's device "
        "list decides: T-Mobile T-Satellite",
        "26203                                           national   unlikely    other network of the SIM's country, "
        "usually no national roaming",
        "23410                                           roaming    maybe       abroad, needs roaming in the contract",
    ]
    assert out[i + 6] == "rc=0"
    assert "call=org.plinius.Satellite /org/plinius/Satellite org.plinius.Satellite Scan" in r.stdout


def test_scan_cli_failure(sat):
    r = sat(pre=[f"{SCAN_KEY}|!Only the user of the unlocked phone may scan for networks"], args=("--scan",))
    assert "rc=1" in r.stdout
    assert "Scan failed: Only the user of the unlocked phone may scan for networks" in r.stderr


def test_scan_cli_without_bus(sat):
    r = sat(args=("--scan",), env={"FAKE_NO_SYSTEM_BUS": "1"})
    assert "rc=1" in r.stdout and "Scanning" not in r.stdout
    assert "Scan failed: fake: no bus" in r.stderr


# ---- plinius-satellite-notify ----

@pytest.fixture
def notify(built):
    d, _, exe = built

    def go(*events, env=None):
        e = dict(os.environ)
        e.update(env or {})
        return run(exe, "main", "--", *events, env=e)
    return go


NOTIFY_OK = "reply|Notify /org/freedesktop/Notifications org.freedesktop.Notifications|(uint32 7,)"


def satellite(provider="T-Mobile T-Satellite", services=("sms",), active=True):
    return [f"prop|Active|{str(active).lower()}", f"prop|Provider|'{provider}'",
            f"prop|Services|@as {list(services)}"]


def notes(out):
    return [c for c in lines(out, "call=") if " Notify(" in c]


def test_notify_main_wiring(notify):
    out = notify("sigterm").stdout
    # flags 4: G_DBUS_PROXY_FLAGS_DO_NOT_AUTO_START
    assert "proxy system org.plinius.Satellite /org/plinius/Satellite org.plinius.Satellite flags=4" in out
    assert "connect g-properties-changed" in out and "connect notify::g-name-owner" in out
    assert lines(out, "subscribe ") == ["sender=org.freedesktop.Notifications iface=org.freedesktop.Notifications "
                                        "member=NotificationClosed path=/org/freedesktop/Notifications"]
    assert notes(out) == []
    assert out.split("loop\n", 1)[1].splitlines() == ["quit", "flushed", "rc=0"]


def test_notify_without_session_bus(notify):
    r = notify(env={"FAKE_NO_SESSION_BUS": "1"})
    assert "rc=1" in r.stdout and "No session bus: fake: no bus" in r.stderr


def test_notify_without_proxy(notify):
    r = notify(env={"FAKE_NO_PROXY": "1"})
    assert "rc=1" in r.stdout and "No connection to plinius-satellite: fake: no proxy" in r.stderr


def test_notification_on_the_satellite(notify):
    out = notify(NOTIFY_OK, *satellite(), "changed", "state").stdout
    assert notes(out) == [
        "org.freedesktop.Notifications /org/freedesktop/Notifications org.freedesktop.Notifications Notify("
        "'Satellite', uint32 0, 'plinius-satellite-symbolic', 'Connected via satellite (T-Mobile T-Satellite)', "
        "'Only text messages work. Stay outdoors with a clear view of the sky. Sending can take a few minutes.', "
        "@as [], {'urgency': <byte 0x01>, 'resident': <true>, 'category': <'network.connected'>}, 0)"]
    assert states(out) == ["note=7 shown_for=T-Mobile T-Satellite:sms"]


@pytest.mark.parametrize("services,text", [
    (("sms", "mms", "data", "voice"), "Calls, messages and data work, slower than usual."),
    (("sms", "voice"), "Calls and text messages work."),
    (("sms", "data"), "Text messages and some apps work, slower than usual."),
    (("mms", "data"), "Text messages and some apps work, slower than usual."),
    (("data",), "Some apps work, slower than usual."),
    (("sms", "mms"), "Only text messages work."),
    ((), "Only text messages work."),
])
def test_notification_says_what_works(notify, services, text):
    out = notify(NOTIFY_OK, *satellite(services=services), "owner").stdout
    assert f"', '{text} Stay outdoors" in notes(out)[0]


def test_notification_without_provider_name(notify):
    out = notify(NOTIFY_OK, *satellite(provider=""), "changed").stdout
    assert "'Connected via satellite', " in notes(out)[0]


def test_notification_once_per_satellite(notify):
    out = notify(NOTIFY_OK, *satellite(), "changed", "changed",
                 # The services change: the notification is replaced, not added
                 "prop|Services|@as ['sms', 'data']", "changed", "state").stdout
    n = notes(out)
    assert len(n) == 2 and "'Satellite', uint32 7, " in n[1]
    assert states(out) == ["note=7 shown_for=T-Mobile T-Satellite:sms,data"]


@pytest.mark.parametrize("gone", [["prop|Active|false"], ["prop|Provider|-"], ["prop|Services|-"],
                                  ["prop|Active|-"]])
def test_notification_goes_with_the_satellite(notify, gone):
    out = notify(NOTIFY_OK, *satellite(), "changed", *gone, "changed", "state", "changed").stdout
    assert lines(out, "async=") == ["org.freedesktop.Notifications /org/freedesktop/Notifications "
                                    "org.freedesktop.Notifications CloseNotification(uint32 7,) timeout=2000"]
    assert states(out) == ["note=0 shown_for=-"]


def test_dismissed_notification_stays_away(notify):
    out = notify(NOTIFY_OK, *satellite(), "changed",
                 "closed|9|NotificationClosed", "closed|7|ActionInvoked", "state",
                 "closed|7|NotificationClosed", "state",
                 # The same satellite (another network name): not shown again
                 "changed",
                 # Another satellite: shown
                 "prop|Provider|'Starlink'", "changed", "state", "sigterm").stdout
    s = states(out)
    assert s[0] == "note=7 shown_for=T-Mobile T-Satellite:sms"
    assert s[1] == "note=0 shown_for=T-Mobile T-Satellite:sms"
    assert len(notes(out)) == 2 and "(Starlink)" in notes(out)[1]
    assert s[2] == "note=7 shown_for=Starlink:sms"
    # Closed when the program ends
    assert lines(out, "async=")[-1].endswith("CloseNotification(uint32 7,) timeout=2000")


def test_dismissed_and_satellite_gone(notify):
    out = notify(NOTIFY_OK, *satellite(), "changed", "closed|7|NotificationClosed", "prop|Active|false",
                 "changed", "state", "prop|Active|true", "changed", "state").stdout
    # Nothing to close when the satellite went, the user did; only the second one at exit
    assert len(lines(out, "async=")) == 1
    s = states(out)
    assert s[0] == "note=0 shown_for=-"
    assert s[1] == "note=7 shown_for=T-Mobile T-Satellite:sms"


def test_dismissed_without_provider_property(notify):
    out = notify(NOTIFY_OK, *satellite(), "changed", "closed|7|NotificationClosed", "prop|Provider|-", "changed",
                 "state").stdout
    assert states(out) == ["note=0 shown_for=-"]


def test_notification_service_missing(notify):
    r = notify(*satellite(), "changed", "state")
    assert "Cannot show the notification: fake: nothing at Notify" in r.stderr
    assert states(r.stdout) == ["note=0 shown_for=T-Mobile T-Satellite:sms"]
