/*
 * Harness for plinius-dnsfilter.c as a running daemon: its real main() runs, with the main loop
 * replaced by a command loop on stdin. Between commands nothing happens; commands that wait
 * ("run", "await", the DNS clients) turn the GLib main loop until their answer is there.
 *
 * Faked (everything else is the daemon's own code, with real sockets on 127.0.0.1/::1):
 * - the buses: g_bus_get_sync gives a stand-in system bus only with LIVE_SYSBUS=1; its
 *   NetworkManager (DnsManager Configuration, Connectivity, Metered) and logind (ListUsers)
 *   answers come from the commands below, the caller checks from dsec_fakebus.h. g_bus_own_name
 *   hands the daemon a stand-in connection at once; method calls and properties go through the
 *   vtable it registers, checked against its introspection XML as GDBus would.
 * - timers: recorded, so "fire NAME" runs one now instead of waiting (an idle exit of 120 s);
 *   "clock S" moves the daemon's clocks (g_get_real_time/g_get_monotonic_time) forward
 * - which app asked (appid.c, tested on its own): "app KEY"
 * - interface names (if_indextoname): "ifname INDEX NAME", for hotspot and USB clients
 * The test copy of the daemon (sandboxed_source) has its fixed paths under LIVE_ROOT_DIR and
 * the shared port of hotspot/USB clients on 127.0.0.1 at LIVE_SHARED_PORT.
 *
 * Commands (one output line each; the daemon's exit code follows as "exit N"):
 *   run MS                       turn the loop for MS ms
 *   call SENDER METHOD ARGS|-    a method call ("pending" while unanswered)
 *   await MS                     the answer of the last pending call
 *   get SENDER PROP / set SENDER PROP VALUE / setraw SENDER PROP VALUE (past GDBus' checks)
 *   wait MS                      how long the DNS clients wait for an answer (default 10 s)
 *   uq HEX / uq6 HEX / uqs HEX   UDP question to the port (v4, v6) or the shared port
 *   usend HEX                    the same without waiting
 *   topen N [shared|6] / tsend N HEX / traw N HEX / trecv N / tclose N   TCP clients
 *   fire NAME / timers NAME      run (or list) the daemon's timers with callback NAME
 *   clock SECONDS                the daemon's clocks forward
 *   nm GVARIANT|- / nmbad / conn N / metered N / users U,U|- / nmchanged / userschanged
 *   ifname INDEX NAME / app KEY|- / signals / show WHAT / vanish NAME / namelost / sigterm
 *   resolve NAME [4|6] / resolvesync NAME / reverse ADDR
 *   writefile PATH TEXT (\n escapes) / chmod PATH OCTAL / quit
 *   bus KEY VALUE                see dsec_fakebus.h
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <glib/gstdio.h>
#include <grp.h>
#include <ifaddrs.h>
#include <libsoup/soup.h>
#include <malloc.h>
#include <net/if.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

#include "dsec_fakebus.h"

/* ---- State of the fakes ---- */

typedef struct {
	guint id;
	GSourceFunc fn;
	gpointer data;
	guint ms;
	char name[64];
} LiveTimer;

static struct {
	gboolean quit;
	GVariant *nm_conf; /* aa{sv}, NULL: NetworkManager does not answer */
	gboolean nm_badtype;
	gboolean no_creds; /* the sender left between the bus daemon's two answers */
	gint64 connectivity, metered; /* -1: no answer */
	char users[256];	      /* "1000,1001", "" none, "-": logind does not answer */
	char *app;
	char ifnames[64][IF_NAMESIZE];
	gint64 real_off, mono_off;
	GString *signals;
	GArray *timers;
	guint next_id;
	const GDBusInterfaceVTable *vtable;
	GDBusInterfaceInfo *info;
	GBusNameLostCallback lost;
	int bus_type;
	int dummy;
} live = {.connectivity = 4, .metered = 4};

int live_shared_port = 5300;

#define LIVE_BUS ((GDBusConnection *)&live.dummy)

static gint64 live_real_time(void)
{
	return g_get_real_time() + live.real_off;
}

static gint64 live_mono_time(void)
{
	return g_get_monotonic_time() + live.mono_off;
}

static guint live_timer_add(guint ms, GSourceFunc fn, gpointer data, const char *name, gboolean seconds)
{
	LiveTimer t = {0};

	t.id = seconds ? (g_timeout_add_seconds)(ms, fn, data) : (g_timeout_add)(ms, fn, data);
	t.fn = fn;
	t.data = data;
	t.ms = seconds ? ms * 1000 : ms;
	g_strlcpy(t.name, name, sizeof(t.name));
	if (!live.timers)
		live.timers = g_array_new(FALSE, FALSE, sizeof(LiveTimer));
	g_array_append_val(live.timers, t);
	return t.id;
}

static gboolean live_timer_alive(guint id)
{
	GSource *s = g_main_context_find_source_by_id(NULL, id);

	return s && !g_source_is_destroyed(s);
}

static GVariant *live_get_reply(GVariant *inner)
{
	return g_variant_ref_sink(g_variant_new("(v)", inner));
}

static GVariant *live_call_sync(GDBusConnection *bus, const char *dest, const char *path, const char *iface,
				const char *method, GVariant *params, const GVariantType *reply_type,
				GDBusCallFlags flags, int timeout, GCancellable *cancellable, GError **error)
{
	g_autoptr(GVariant) p = params ? g_variant_ref_sink(params) : NULL;

	if (!g_strcmp0(dest, "org.freedesktop.NetworkManager") && !g_strcmp0(method, "Get")) {
		const char *ifc, *prop;

		g_variant_get(p, "(&s&s)", &ifc, &prop);
		if (!strcmp(prop, "Configuration")) {
			if (live.nm_badtype)
				return live_get_reply(g_variant_new_string("garbage"));
			if (live.nm_conf)
				return live_get_reply(live.nm_conf);
		} else {
			gint64 v = !strcmp(prop, "Connectivity") ? live.connectivity : live.metered;

			if (v == -2)
				return live_get_reply(g_variant_new_string("unknown"));
			if (v >= 0)
				return live_get_reply(g_variant_new_uint32((guint32)v));
		}
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_SERVICE_UNKNOWN, "fake bus: no NetworkManager");
		return NULL;
	}
	if (!g_strcmp0(method, "GetConnectionCredentials") && live.no_creds) {
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_NAME_HAS_NO_OWNER, "fake bus: the sender left");
		return NULL;
	}
	if (!g_strcmp0(method, "ListUsers")) {
		GVariantBuilder b;
		g_auto(GStrv) ids = NULL;

		if (!strcmp(live.users, "-")) {
			g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_SERVICE_UNKNOWN, "fake bus: no logind");
			return NULL;
		}
		g_variant_builder_init(&b, G_VARIANT_TYPE("a(uso)"));
		ids = g_strsplit(live.users, ",", -1);
		for (char **id = ids; *id; id++)
			if (**id)
				g_variant_builder_add(&b, "(uso)", (guint32)g_ascii_strtoull(*id, NULL, 10), "user",
						      "/org/freedesktop/login1/user/_1");
		return g_variant_ref_sink(g_variant_new("(a(uso))", &b));
	}
	return fake_call_sync(bus, dest, path, iface, method, p, reply_type, flags, timeout, cancellable, error);
}

static gboolean live_emit_signal(GDBusConnection *bus, const char *dest, const char *path, const char *iface,
				 const char *signal, GVariant *params, GError **error)
{
	g_autoptr(GVariant) p = g_variant_ref_sink(params);

	if (!live.signals)
		live.signals = g_string_new(NULL);
	if (live.signals->len)
		g_string_append_c(live.signals, ';');
	g_string_append_printf(live.signals, "%s>", dest ? dest : "*");
	if (!strcmp(signal, "PropertiesChanged")) {
		g_autoptr(GVariant) changed = g_variant_get_child_value(p, 1);
		GVariantIter it;
		const char *key;

		g_variant_iter_init(&it, changed);
		while (g_variant_iter_next(&it, "{&sv}", &key, NULL))
			g_string_append(live.signals, key);
	} else {
		g_autoptr(GVariant) entries = g_variant_get_child_value(p, 0);

		g_string_append_printf(live.signals, "%s:%zu", signal, g_variant_n_children(entries));
	}
	fb.signals++;
	return TRUE;
}

static guint live_watch_name(GDBusConnection *bus, const char *name, GBusNameWatcherFlags flags,
			     GBusNameAppearedCallback appeared, GBusNameVanishedCallback vanished, gpointer data,
			     GDestroyNotify destroy)
{
	if (!live.signals)
		live.signals = g_string_new(NULL);
	g_string_append_printf(live.signals, "%swatch:%s", live.signals->len ? ";" : "", name);
	return ++live.next_id;
}

static void live_unwatch_name(guint id)
{
	if (!live.signals)
		live.signals = g_string_new(NULL);
	g_string_append_printf(live.signals, "%sunwatch", live.signals->len ? ";" : "");
}

static GDBusConnection *live_bus_get(GBusType type, GCancellable *cancellable, GError **error)
{
	if (g_strcmp0(g_getenv("LIVE_SYSBUS"), "1")) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND, "fake: no system bus");
		return NULL;
	}
	return LIVE_BUS;
}

static guint live_own_name(GBusType type, const char *name, GBusNameOwnerFlags flags, GBusAcquiredCallback acquired,
			   GBusNameAcquiredCallback name_acquired, GBusNameLostCallback lost, gpointer data,
			   GDestroyNotify destroy)
{
	live.bus_type = type;
	live.lost = lost;
	acquired(LIVE_BUS, name, data);
	return 1;
}

static guint live_register_object(GDBusConnection *bus, const char *path, GDBusInterfaceInfo *info,
				  const GDBusInterfaceVTable *vtable, gpointer data, GDestroyNotify destroy,
				  GError **error)
{
	live.vtable = vtable;
	live.info = g_dbus_interface_info_ref(info);
	return 1;
}

static guint live_signal_subscribe(GDBusConnection *bus, const char *sender, const char *iface, const char *member,
				   const char *path, const char *arg0, GDBusSignalFlags flags,
				   GDBusSignalCallback callback, gpointer data, GDestroyNotify destroy)
{
	return ++live.next_id;
}

static void live_loop_run(GMainLoop *loop);

static void live_loop_quit(GMainLoop *loop)
{
	live.quit = TRUE;
}

static char *live_if_indextoname(unsigned index, char *name)
{
	if (index >= G_N_ELEMENTS(live.ifnames) || !live.ifnames[index][0]) {
		errno = ENXIO;
		return NULL;
	}
	return strcpy(name, live.ifnames[index]);
}

static unsigned live_if_nametoindex(const char *name)
{
	for (guint i = 1; i < G_N_ELEMENTS(live.ifnames); i++)
		if (!strcmp(live.ifnames[i], name))
			return i;
	return if_nametoindex(name);
}

static char *live_appid_for_peer(int protocol, const struct sockaddr_storage *peer)
{
	return g_strdup(live.app);
}

#undef g_dbus_connection_call_sync
#undef g_dbus_connection_emit_signal
#define g_dbus_connection_call_sync live_call_sync
#define g_dbus_connection_emit_signal live_emit_signal
#define g_bus_watch_name_on_connection live_watch_name
#define g_bus_unwatch_name live_unwatch_name
#define g_bus_get_sync live_bus_get
#define g_bus_own_name live_own_name
#define g_dbus_connection_register_object live_register_object
#define g_dbus_connection_signal_subscribe live_signal_subscribe
#define g_main_loop_run live_loop_run
#define g_main_loop_quit live_loop_quit
#undef g_timeout_add
#undef g_timeout_add_seconds
#define g_timeout_add(ms, fn, data) live_timer_add(ms, fn, data, #fn, FALSE)
#define g_timeout_add_seconds(s, fn, data) live_timer_add(s, fn, data, #fn, TRUE)
#define g_get_real_time live_real_time
#define g_get_monotonic_time live_mono_time
#define if_indextoname live_if_indextoname
#define if_nametoindex live_if_nametoindex
#define appid_for_peer live_appid_for_peer

#define main dnsfilter_main
#include "plinius-dnsfilter.c"
#undef main
#undef g_timeout_add
#undef g_get_monotonic_time

/* ---- The command loop ---- */

static guint wait_ms = 10000;
static GPtrArray *invocations;
static FakeInv *pending;
static GDBusMethodInfo *pending_info;

static void turn(void)
{
	while (g_main_context_iteration(NULL, FALSE))
		;
}

static void run_for(guint ms)
{
	gint64 end = g_get_monotonic_time() + (gint64)ms * 1000;

	while (g_get_monotonic_time() < end && !live.quit) {
		turn();
		g_usleep(1000);
	}
	turn();
}

/* Turns the loop until @fd is readable; FALSE after wait_ms */
static gboolean wait_readable(int fd)
{
	gint64 end = g_get_monotonic_time() + (gint64)wait_ms * 1000;
	struct pollfd p = {fd, POLLIN, 0};

	for (;;) {
		turn();
		if (poll(&p, 1, 1) > 0)
			return TRUE;
		if (g_get_monotonic_time() >= end)
			return FALSE;
	}
}

static GByteArray *unhex(const char *hex)
{
	GByteArray *b = g_byte_array_new();

	for (; hex[0] && hex[1]; hex += 2) {
		guint8 v = (guint8)(g_ascii_xdigit_value(hex[0]) * 16 + g_ascii_xdigit_value(hex[1]));

		g_byte_array_append(b, &v, 1);
	}
	return b;
}

static void print_hex(const char *what, const guint8 *data, gsize len)
{
	printf("%s ", what);
	for (gsize i = 0; i < len; i++)
		printf("%02x", data[i]);
	putchar('\n');
}

static int udp_client(int family, guint16 port)
{
	int fd = socket(family, SOCK_DGRAM | SOCK_CLOEXEC, 0);

	if (family == AF_INET) {
		struct sockaddr_in a = {.sin_family = AF_INET, .sin_port = htons(port)};

		inet_pton(AF_INET, "127.0.0.1", &a.sin_addr);
		connect(fd, (struct sockaddr *)&a, sizeof(a));
	} else {
		struct sockaddr_in6 a = {.sin6_family = AF_INET6, .sin6_port = htons(port), .sin6_addr = IN6ADDR_LOOPBACK_INIT};

		connect(fd, (struct sockaddr *)&a, sizeof(a));
	}
	return fd;
}

static void udp_question(int family, guint16 port, const char *hex, gboolean wait)
{
	g_autoptr(GByteArray) q = unhex(hex);
	int fd = udp_client(family, port);
	guint8 buf[65536];

	if (send(fd, q->data, q->len, 0) < 0) {
		printf("error %s\n", g_strerror(errno));
	} else if (!wait) {
		turn();
		puts("ok");
	} else if (wait_readable(fd)) {
		ssize_t n = recv(fd, buf, sizeof(buf), 0);

		if (n < 0)
			printf("error %s\n", g_strerror(errno));
		else
			print_hex("reply", buf, n);
	} else {
		puts("noreply");
	}
	close(fd);
}

static int tcp_fds[160];

/* @how: "phone", "6" (::1), "shared" or "shared6" (the port of hotspot/USB clients); @from: the
 * client's own address (127.0.0.x) or "" */
static void tcp_open(int n, const char *how, const char *from)
{
	gboolean v6 = !strcmp(how, "6") || !strcmp(how, "shared6");
	guint16 port = g_str_has_prefix(how, "shared") ? live_shared_port : self.port;
	int fd = socket(v6 ? AF_INET6 : AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);

	if (*from) {
		struct sockaddr_in b = {.sin_family = AF_INET};

		inet_pton(AF_INET, from, &b.sin_addr);
		if (bind(fd, (struct sockaddr *)&b, sizeof(b)) < 0)
			goto fail;
	}
	if (v6) {
		struct sockaddr_in6 a = {.sin6_family = AF_INET6, .sin6_port = htons(port), .sin6_addr = IN6ADDR_LOOPBACK_INIT};

		if (connect(fd, (struct sockaddr *)&a, sizeof(a)) < 0)
			goto fail;
	} else {
		struct sockaddr_in a = {.sin_family = AF_INET, .sin_port = htons(port)};

		inet_pton(AF_INET, "127.0.0.1", &a.sin_addr);
		if (connect(fd, (struct sockaddr *)&a, sizeof(a)) < 0)
			goto fail;
	}
	tcp_fds[n] = fd;
	turn();
	puts("ok");
	return;
fail:
	printf("error %s\n", g_strerror(errno));
	close(fd);
}

/* UDP clients that stay open: a question now, its answer later ("ukeep", "urecv") */
static int udp_fds[2048];

static void udp_receive(int n)
{
	guint8 buf[65536];

	if (udp_fds[n] < 0) {
		puts("<no such client>");
		return;
	}
	if (wait_readable(udp_fds[n])) {
		ssize_t r = recv(udp_fds[n], buf, sizeof(buf), 0);

		if (r < 0)
			printf("error %s\n", g_strerror(errno));
		else
			print_hex("reply", buf, r);
	} else {
		puts("noreply");
	}
}

/* One framed answer, "eof" when the daemon closed the connection, "noreply" after wait_ms */
static void tcp_receive(int n)
{
	int fd = tcp_fds[n];
	guint8 len[2];
	g_autofree guint8 *body = NULL;
	gsize got = 0, want = 2;
	guint8 *into = len;

	for (;;) {
		ssize_t r;

		if (!wait_readable(fd)) {
			puts("noreply");
			return;
		}
		r = recv(fd, into + got, want - got, MSG_DONTWAIT);
		if (r < 0 && (errno == EAGAIN || errno == EINTR))
			continue;
		if (r <= 0) {
			puts("eof");
			return;
		}
		got += r;
		if (got < want)
			continue;
		if (into == len) {
			want = (len[0] << 8) | len[1];
			body = g_malloc(want ? want : 1);
			into = body;
			got = 0;
			if (want)
				continue;
		}
		print_hex("reply", body, want);
		return;
	}
}

static void print_inv(FakeInv *inv, GDBusMethodInfo *mi)
{
	if (inv->answered && !inv->error_name && mi) {
		GString *sig = g_string_new("(");
		const char *got = inv->value ? g_variant_get_type_string(inv->value) : "()";

		for (guint i = 0; mi->out_args && mi->out_args[i]; i++)
			g_string_append(sig, mi->out_args[i]->signature);
		g_string_append_c(sig, ')');
		/* GDBus refuses to send a reply of the wrong type */
		if (strcmp(sig->str, got)) {
			printf("BADREPLY %s instead of %s\n", got, sig->str);
			g_string_free(sig, TRUE);
			return;
		}
		g_string_free(sig, TRUE);
	}
	/* With types, so the test can read every value back */
	if (inv->answered && !inv->error_name && inv->value) {
		g_autofree char *text = g_variant_print(inv->value, TRUE);

		printf("ok %s\n", text);
		return;
	}
	fake_inv_print(inv);
}

static GVariant *parse_value(const char *text, GError **error)
{
	return g_variant_parse(NULL, text, NULL, NULL, error);
}

static void method_call(const char *sender, const char *method, const char *args)
{
	GDBusMethodInfo *mi = g_dbus_interface_info_lookup_method(live.info, method);
	g_autoptr(GError) error = NULL;
	GVariant *params;
	FakeInv *inv;
	GString *sig;

	if (!mi) {
		puts("error org.freedesktop.DBus.Error.UnknownMethod: not in the introspection");
		return;
	}
	params = !strcmp(args, "-") ? g_variant_new("()") : parse_value(args, &error);
	if (!params) {
		printf("<bad arguments: %s>\n", error->message);
		return;
	}
	g_variant_ref_sink(params);
	sig = g_string_new("(");
	for (guint i = 0; mi->in_args && mi->in_args[i]; i++)
		g_string_append(sig, mi->in_args[i]->signature);
	g_string_append_c(sig, ')');
	if (strcmp(sig->str, g_variant_get_type_string(params))) {
		/* GDBus answers this itself, the daemon never sees it */
		printf("error org.freedesktop.DBus.Error.InvalidArgs: %s for %s\n", g_variant_get_type_string(params),
		       sig->str);
		g_string_free(sig, TRUE);
		g_variant_unref(params);
		return;
	}
	g_string_free(sig, TRUE);
	inv = g_new0(FakeInv, 1);
	fake_inv_reset(inv, mi->name, g_variant_ref(params));
	g_ptr_array_add(invocations, inv);
	live.vtable->method_call(LIVE_BUS, sender, OBJECT_PATH, INTERFACE, mi->name, params,
				 (GDBusMethodInvocation *)inv, NULL);
	g_variant_unref(params);
	if (!inv->answered) {
		pending = inv;
		pending_info = mi;
	}
	print_inv(inv, mi);
}

static void property(const char *sender, const char *prop, const char *value, gboolean checked)
{
	GDBusPropertyInfo *pi = g_dbus_interface_info_lookup_property(live.info, prop);
	g_autoptr(GError) error = NULL;

	if (!value) {
		g_autoptr(GVariant) v = NULL;
		g_autofree char *text = NULL;

		if (!pi) {
			puts("error org.freedesktop.DBus.Error.InvalidArgs: no such property");
			return;
		}
		v = live.vtable->get_property(LIVE_BUS, sender, OBJECT_PATH, INTERFACE, prop, &error, NULL);
		if (!v) {
			printf("error %s\n", error ? error->message : "no value");
			return;
		}
		g_variant_ref_sink(v);
		if (strcmp(g_variant_get_type_string(v), pi->signature)) {
			printf("BADREPLY %s instead of %s\n", g_variant_get_type_string(v), pi->signature);
			return;
		}
		text = g_variant_print(v, TRUE);
		printf("ok %s\n", text);
		return;
	}
	{
		g_autoptr(GVariant) v = parse_value(value, &error);

		if (!v) {
			printf("<bad value: %s>\n", error->message);
			return;
		}
		g_variant_ref_sink(v);
		if (checked) {
			if (!pi) {
				puts("error org.freedesktop.DBus.Error.InvalidArgs: no such property");
				return;
			}
			if (!(pi->flags & G_DBUS_PROPERTY_INFO_FLAGS_WRITABLE)) {
				puts("error org.freedesktop.DBus.Error.PropertyReadOnly: not writable");
				return;
			}
			if (strcmp(g_variant_get_type_string(v), pi->signature)) {
				printf("error org.freedesktop.DBus.Error.InvalidArgs: %s for %s\n", g_variant_get_type_string(v),
				       pi->signature);
				return;
			}
		}
		if (live.vtable->set_property(LIVE_BUS, sender, OBJECT_PATH, INTERFACE, prop, v, &error, NULL))
			puts("ok");
		else
			printf("error %s\n", error ? error->message : "refused");
	}
}

static void fire(const char *name)
{
	guint fired = 0, n = live.timers ? live.timers->len : 0;

	/* Only those there now: a callback may add timers (the next server's) */
	for (guint i = 0; i < n; i++) {
		LiveTimer t = g_array_index(live.timers, LiveTimer, i);

		if (strcmp(t.name, name) || !live_timer_alive(t.id))
			continue;
		fired++;
		if (!t.fn(t.data) && live_timer_alive(t.id))
			g_source_remove(t.id);
	}
	printf("fired %u\n", fired);
}

static void timers(const char *name)
{
	GString *out = g_string_new(NULL);

	for (guint i = 0; live.timers && i < live.timers->len; i++) {
		LiveTimer *t = &g_array_index(live.timers, LiveTimer, i);

		if (!strcmp(t->name, name) && live_timer_alive(t->id))
			g_string_append_printf(out, "%s%u", out->len ? "," : "", t->ms);
	}
	printf("timers %s\n", out->len ? out->str : "-");
	g_string_free(out, TRUE);
}

typedef struct {
	gboolean done;
	GList *addrs;
	GError *error;
} Resolved;

static void on_resolved(GObject *source, GAsyncResult *res, gpointer data)
{
	Resolved *r = data;

	r->addrs = g_resolver_lookup_by_name_with_flags_finish(G_RESOLVER(source), res, &r->error);
	r->done = TRUE;
}

static void resolve(const char *name, int which)
{
	Resolved r = {0};
	gint64 end = g_get_monotonic_time() + (gint64)wait_ms * 1000;
	GResolverNameLookupFlags flags = which == 4   ? G_RESOLVER_NAME_LOOKUP_FLAGS_IPV4_ONLY
					 : which == 6 ? G_RESOLVER_NAME_LOOKUP_FLAGS_IPV6_ONLY
						      : G_RESOLVER_NAME_LOOKUP_FLAGS_DEFAULT;
	g_autoptr(GResolver) resolver = g_resolver_get_default();

	g_resolver_lookup_by_name_with_flags_async(resolver, name, flags, NULL, on_resolved, &r);
	while (!r.done && g_get_monotonic_time() < end) {
		turn();
		g_usleep(1000);
	}
	if (!r.done) {
		puts("timeout");
		return;
	}
	if (r.error) {
		printf("error %s\n", r.error->message);
		g_error_free(r.error);
		return;
	}
	printf("addrs");
	for (GList *l = r.addrs; l; l = l->next) {
		g_autofree char *t = g_inet_address_to_string(l->data);

		printf("%s%s", l == r.addrs ? " " : ",", t);
	}
	putchar('\n');
	g_resolver_free_addresses(r.addrs);
}

static void show(const char *what)
{
	if (!strcmp(what, "status"))
		printf("%s\n", self.status ? self.status : "-");
	else if (!strcmp(what, "listening"))
		printf("%d %d\n", self.listening, self.udp_fds->len);
	else if (!strcmp(what, "requests"))
		printf("%u %u\n", self.requests, self.requests_per ? g_hash_table_size(self.requests_per) : 0);
	else if (!strcmp(what, "tcp"))
		printf("%u %u %u\n", self.tcp_clients, self.tcp_shared, g_hash_table_size(self.tcp_peers));
	else if (!strcmp(what, "cache"))
		printf("%u\n", g_hash_table_size(self.cache));
	else if (!strcmp(what, "links"))
		printf("%u\n", self.links->len);
	else if (!strcmp(what, "custom"))
		printf("%u\n", self.custom->len);
	else if (!strcmp(what, "log"))
		printf("%u %u\n", self.log_count, self.log_cap);
	else if (!strcmp(what, "watchers"))
		printf("%u\n", g_hash_table_size(self.watchers));
	else if (!strcmp(what, "connectivity"))
		printf("%u %u\n", self.connectivity, self.metered);
	else if (!strcmp(what, "bus"))
		printf("%s\n", live.bus_type == G_BUS_TYPE_SESSION ? "session" : "system");
	else if (!strcmp(what, "error"))
		printf("%s\n", self.last_error ? self.last_error : "-");
	else if (!strcmp(what, "hours"))
		printf("%u %u\n", self.hours->len, self.days->len);
	else if (!strcmp(what, "upstreams")) {
		GString *out = g_string_new(NULL);

		for (guint i = 0; i < self.custom->len; i++) {
			Upstream *u = g_ptr_array_index(self.custom, i);

			g_string_append_printf(out, "%s%s=%u", out->len ? "," : "", u->spec, u->failures);
		}
		for (guint i = 0; i < self.links->len; i++) {
			NetLink *l = g_ptr_array_index(self.links, i);

			for (guint k = 0; k < l->servers->len; k++) {
				Upstream *u = g_ptr_array_index(l->servers, k);

				g_string_append_printf(out, "%s%s=%u", out->len ? "," : "", u->spec, u->failures);
			}
		}
		printf("%s\n", out->len ? out->str : "-");
		g_string_free(out, TRUE);
	} else
		printf("<unknown: %s>\n", what);
}

#define IS(word) (!strcmp(cmd, word))

/* For "until": no TCP client left (or one there), no question in flight, no list downloading */
static gboolean condition(const char *what)
{
	if (!strcmp(what, "tcp0"))
		return self.tcp_clients + self.tcp_shared == 0;
	if (!strcmp(what, "tcp1"))
		return self.tcp_clients + self.tcp_shared > 0;
	if (!strcmp(what, "requests0"))
		return self.requests == 0;
	if (!strcmp(what, "lists-idle")) {
		for (guint i = 0; i < self.lists->len; i++)
			if (((List *)g_ptr_array_index(self.lists, i))->downloading)
				return FALSE;
		return TRUE;
	}
	return FALSE;
}

static void live_loop_run(GMainLoop *loop)
{
	static char line[1 << 20];

	invocations = g_ptr_array_new();
	while (!live.quit && fgets(line, sizeof line, stdin)) {
		char a[4096] = "", b[4096] = "", c[8192] = "";
		long n;
		int off = 0;
		char cmd[32];

		line[strcspn(line, "\n")] = '\0';
		cmd[0] = '\0';
		sscanf(line, "%31s", cmd);
		if (fb_command(line))
			continue;
		if (IS("run") && sscanf(line, "run %ld", &n) == 1) {
			run_for((guint)n);
			puts("ok");
		} else if (IS("call") && sscanf(line, "call %4095s %4095s %n", a, b, &off) == 2 && off) {
			method_call(a, b, line + off);
		} else if (IS("callraw") && sscanf(line, "callraw %4095s %4095s", a, b) == 2) {
			FakeInv *inv = g_new0(FakeInv, 1);

			fake_inv_reset(inv, b, g_variant_new("()"));
			live.vtable->method_call(LIVE_BUS, a, OBJECT_PATH, INTERFACE, b, inv->params,
						 (GDBusMethodInvocation *)inv, NULL);
			fake_inv_print(inv);
		} else if (IS("until") && sscanf(line, "until %4095s %ld", a, &n) == 2) {
			gint64 end = g_get_monotonic_time() + n * 1000;
			gboolean ok;

			while (!(ok = condition(a)) && g_get_monotonic_time() < end) {
				turn();
				g_usleep(1000);
			}
			puts(ok ? "ok" : "timeout");
		} else if (IS("await") && sscanf(line, "await %ld", &n) == 1) {
			gint64 end = g_get_monotonic_time() + n * 1000;

			while (pending && !pending->answered && g_get_monotonic_time() < end) {
				turn();
				g_usleep(1000);
			}
			if (!pending)
				puts("<nothing pending>");
			else
				print_inv(pending, pending_info);
		} else if (IS("set") && sscanf(line, "set %4095s %4095s %8191[^\n]", a, b, c) == 3) {
			property(a, b, c, TRUE);
		} else if (IS("setraw") && sscanf(line, "setraw %4095s %4095s %8191[^\n]", a, b, c) == 3) {
			property(a, b, c, FALSE);
		} else if (IS("get") && sscanf(line, "get %4095s %4095s", a, b) == 2) {
			property(a, b, NULL, TRUE);
		} else if (IS("wait") && sscanf(line, "wait %ld", &n) == 1) {
			wait_ms = (guint)n;
			puts("ok");
		} else if (IS("uq") && sscanf(line, "uq %4095s", a) == 1) {
			udp_question(AF_INET, self.port, line + 3, TRUE);
		} else if (IS("uq6") && sscanf(line, "uq6 %4095s", a) == 1) {
			udp_question(AF_INET6, self.port, line + 4, TRUE);
		} else if (IS("uqs") && sscanf(line, "uqs %4095s", a) == 1) {
			udp_question(AF_INET, live_shared_port, line + 4, TRUE);
		} else if (IS("uqs6") && sscanf(line, "uqs6 %4095s", a) == 1) {
			udp_question(AF_INET6, live_shared_port, line + 5, TRUE);
		} else if (IS("ukeep") && sscanf(line, "ukeep %ld %4095s", &n, a) == 2) {
			g_autoptr(GByteArray) q = unhex(a);

			if (udp_fds[n] < 0)
				udp_fds[n] = udp_client(AF_INET, self.port);
			if (send(udp_fds[n], q->data, q->len, 0) < 0)
				printf("error %s\n", g_strerror(errno));
			else
				puts("ok");
			turn();
		} else if (IS("urecv") && sscanf(line, "urecv %ld", &n) == 1) {
			udp_receive((int)n);
		} else if (IS("usend") && sscanf(line, "usend %4095s", a) == 1) {
			udp_question(AF_INET, self.port, line + 6, FALSE);
		} else if (IS("topen") && sscanf(line, "topen %ld %4095s %4095s", &n, a, b) >= 1) {
			tcp_open((int)n, a[0] ? a : "phone", b);
		} else if ((IS("tsend") || IS("traw")) && sscanf(line, "%*s %ld %4095s", &n, a) == 2) {
			g_autoptr(GByteArray) q = unhex(a);
			GByteArray *out = g_byte_array_new();

			if (line[1] == 's') {
				guint8 l[2] = {q->len >> 8, q->len & 0xff};

				g_byte_array_append(out, l, 2);
			}
			g_byte_array_append(out, q->data, q->len);
			if (send(tcp_fds[n], out->data, out->len, MSG_NOSIGNAL) < 0)
				printf("error %s\n", g_strerror(errno));
			else
				puts("ok");
			g_byte_array_unref(out);
			turn();
		} else if (IS("trecv") && sscanf(line, "trecv %ld", &n) == 1) {
			tcp_receive((int)n);
		} else if (IS("tclose") && sscanf(line, "tclose %ld", &n) == 1) {
			close(tcp_fds[n]);
			tcp_fds[n] = -1;
			turn();
			puts("ok");
		} else if (IS("fire") && sscanf(line, "fire %4095s", a) == 1) {
			fire(a);
		} else if (IS("timers") && sscanf(line, "timers %4095s", a) == 1) {
			timers(a);
		} else if (IS("clock") && sscanf(line, "clock %ld", &n) == 1) {
			live.real_off += n * G_USEC_PER_SEC;
			live.mono_off += n * G_USEC_PER_SEC;
			puts("ok");
		} else if (IS("nm") && !strncmp(line, "nm ", 3)) {
			g_autoptr(GError) error = NULL;

			g_clear_pointer(&live.nm_conf, g_variant_unref);
			live.nm_badtype = FALSE;
			if (strcmp(line + 3, "-")) {
				live.nm_conf = g_variant_parse(G_VARIANT_TYPE("aa{sv}"), line + 3, NULL, NULL, &error);
				if (!live.nm_conf) {
					printf("<bad nm: %s>\n", error->message);
					continue;
				}
				g_variant_ref_sink(live.nm_conf);
			}
			puts("ok");
		} else if (!strcmp(line, "nocreds")) {
			live.no_creds = TRUE;
			puts("ok");
		} else if (!strcmp(line, "nmbad")) {
			live.nm_badtype = TRUE;
			puts("ok");
		} else if (IS("conn") && sscanf(line, "conn %ld", &n) == 1) {
			live.connectivity = n;
			puts("ok");
		} else if (IS("metered") && sscanf(line, "metered %ld", &n) == 1) {
			live.metered = n;
			puts("ok");
		} else if (IS("users") && sscanf(line, "users %255s", live.users) == 1) {
			if (!strcmp(live.users, "none"))
				live.users[0] = '\0';
			puts("ok");
		} else if (!strcmp(line, "nmchanged")) {
			on_nm_changed(LIVE_BUS, ":1.1", "/org/freedesktop/NetworkManager", "org.freedesktop.DBus.Properties",
				      "PropertiesChanged", NULL, NULL);
			puts("ok");
		} else if (!strcmp(line, "userschanged")) {
			on_users_changed(LIVE_BUS, ":1.2", "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
					 "UserNew", NULL, NULL);
			puts("ok");
		} else if (IS("ifname") && sscanf(line, "ifname %ld %15s", &n, a) == 2) {
			g_strlcpy(live.ifnames[n], a, IF_NAMESIZE);
			puts("ok");
		} else if (IS("app") && sscanf(line, "app %4095s", a) == 1) {
			g_free(live.app);
			live.app = strcmp(a, "-") ? g_strdup(a) : NULL;
			puts("ok");
		} else if (!strcmp(line, "signals")) {
			printf("signals %s\n", live.signals && live.signals->len ? live.signals->str : "-");
			if (live.signals)
				g_string_truncate(live.signals, 0);
		} else if (IS("show") && sscanf(line, "show %4095s", a) == 1) {
			show(a);
		} else if (IS("vanish") && sscanf(line, "vanish %4095s", a) == 1) {
			on_watcher_vanished(LIVE_BUS, a, NULL);
			puts("ok");
		} else if (!strcmp(line, "namelost")) {
			live.lost(LIVE_BUS, BUS_NAME, NULL);
			puts("ok");
		} else if (!strcmp(line, "sigterm")) {
			raise(SIGTERM);
			run_for(50);
			puts("ok");
		} else if (IS("resolve") && sscanf(line, "resolve %4095s %ld", a, &n) >= 1) {
			resolve(a, sscanf(line, "resolve %4095s %ld", a, &n) == 2 ? (int)n : 0);
		} else if (IS("resolvesync") && sscanf(line, "resolvesync %4095s", a) == 1) {
			g_autoptr(GError) error = NULL;
			g_autoptr(GResolver) resolver = g_resolver_get_default();
			GList *l = g_resolver_lookup_by_name(resolver, a, NULL, &error);

			if (l) {
				puts("addrs");
				g_resolver_free_addresses(l);
			} else {
				printf("error %s\n", error->message);
			}
		} else if (IS("resolvesyncflags") && sscanf(line, "resolvesyncflags %4095s", a) == 1) {
			g_autoptr(GError) error = NULL;
			g_autoptr(GResolver) resolver = g_resolver_get_default();
			GList *l = g_resolver_lookup_by_name_with_flags(resolver, a, G_RESOLVER_NAME_LOOKUP_FLAGS_IPV4_ONLY,
									NULL, &error);

			if (l) {
				puts("addrs");
				g_resolver_free_addresses(l);
			} else {
				printf("error %s\n", error->message);
			}
		} else if (IS("getraw") && sscanf(line, "getraw %4095s %4095s", a, b) == 2) {
			g_autoptr(GError) error = NULL;
			GVariant *v = live.vtable->get_property(LIVE_BUS, a, OBJECT_PATH, INTERFACE, b, &error, NULL);

			if (v) {
				g_autofree char *text = g_variant_print(g_variant_ref_sink(v), TRUE);

				printf("ok %s\n", text);
				g_variant_unref(v);
			} else {
				printf("none %s\n", error ? error->message : "-");
			}
		} else if (IS("reverse") && sscanf(line, "reverse %4095s", a) == 1) {
			g_autoptr(GError) error = NULL;
			g_autoptr(GResolver) resolver = g_resolver_get_default();
			g_autoptr(GInetAddress) ia = g_inet_address_new_from_string(a);
			g_autofree char *name = g_resolver_lookup_by_address(resolver, ia, NULL, &error);

			printf("%s %s\n", name ? "name" : "error", name ? name : error->message);
		} else if (IS("writefile") && sscanf(line, "writefile %4095s %n", a, &off) == 1 && off) {
			g_autofree char *text = g_strcompress(line + off);

			puts(g_file_set_contents(a, text, -1, NULL) ? "ok" : "error");
		} else if (IS("readlink") && sscanf(line, "readlink %4095s", a) == 1) {
			g_autofree char *target = g_file_read_link(a, NULL);

			printf("link %s\n", target ? target : "-");
		} else if (IS("unlink") && sscanf(line, "unlink %4095s", a) == 1) {
			puts(unlink(a) == 0 ? "ok" : "error");
		} else if (IS("chmod") && sscanf(line, "chmod %4095s %lo", a, &n) == 2) {
			puts(chmod(a, (mode_t)n) == 0 ? "ok" : "error");
		} else if (!strcmp(line, "quit")) {
			puts("ok");
			break;
		} else {
			printf("<unknown command: %.60s>\n", line);
		}
		fflush(stdout);
	}
}

int main(int argc, char **argv)
{
	const char *ca = g_getenv("LIVE_CA");
	const char *port = g_getenv("LIVE_SHARED_PORT");
	int status;

	setvbuf(stdout, NULL, _IOLBF, 0);
	for (guint i = 0; i < G_N_ELEMENTS(tcp_fds); i++)
		tcp_fds[i] = -1;
	for (guint i = 0; i < G_N_ELEMENTS(udp_fds); i++)
		udp_fds[i] = -1;
	/* A thousand questions in flight hold a thousand upstream sockets, the clients as many */
	{
		struct rlimit rl;

		if (getrlimit(RLIMIT_NOFILE, &rl) == 0) {
			rl.rlim_cur = MIN(rl.rlim_max, 8192);
			setrlimit(RLIMIT_NOFILE, &rl);
		}
	}
	if (port)
		live_shared_port = atoi(port);
	if (g_getenv("LIVE_USERS"))
		g_strlcpy(live.users, g_getenv("LIVE_USERS"), sizeof(live.users));
	if (g_getenv("LIVE_NM")) {
		live.nm_conf = g_variant_ref_sink(g_variant_parse(G_VARIANT_TYPE("aa{sv}"), g_getenv("LIVE_NM"), NULL, NULL, NULL));
		g_assert(live.nm_conf);
	}
	if (g_getenv("LIVE_CONNECTIVITY"))
		live.connectivity = g_ascii_strtoll(g_getenv("LIVE_CONNECTIVITY"), NULL, 10);
	if (g_getenv("LIVE_METERED"))
		live.metered = g_ascii_strtoll(g_getenv("LIVE_METERED"), NULL, 10);
	/* The test's own CA stands for the public ones: DoT, DoH and list servers in the test */
	if (ca) {
		g_autoptr(GError) error = NULL;
		g_autoptr(GTlsDatabase) db = g_tls_file_database_new(ca, &error);

		g_assert_no_error(error);
		g_tls_backend_set_default_database(g_tls_backend_get_default(), db);
	}
	status = dnsfilter_main(argc, argv);
	printf("exit %d\n", status);
	return 0;
}
