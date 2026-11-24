/*
 * dhw_scripted_gdbus.h: a scripted stand-in for the GDBus calls of the hardware daemons
 * (tests/daemons-hw: plinius-suspend, plinius-wake, plinius-dictated, sensor-proxy-bridge).
 * Include it after gio.h and before the daemon's .c file. Nothing reaches a real bus:
 *
 *   g_bus_get_sync / g_bus_own_name    -> sg.bus, a plain GObject (NULL/name lost if scripted)
 *   g_dbus_connection_call_sync        -> the reply scripted with sg_reply("Method", "(GVariant text)")
 *   g_dbus_connection_call (async)     -> the same reply, delivered on the next main loop iteration
 *   g_dbus_connection_emit_signal      -> printed: "signal DEST PATH IFACE NAME PARAMS"
 *   g_dbus_connection_signal_subscribe -> kept; sg_deliver() calls the handler
 *   g_dbus_connection_register_object  -> kept; sg_invoke() / sg_get() / sg_set() call the vtable
 *   g_bus_watch_name_on_connection     -> kept; sg_vanish() calls the vanished handler
 *   GDBusProxy                         -> SgProxy: cached properties, name owner and the handlers of
 *                                         g-properties-changed, g-signal and notify::g-name-owner
 *
 * Every call to the bus is printed as one line, so a test sees what the daemon asked and sent.
 * A reply text starting with '!' is an error with that message. The daemon's casts to GDBus
 * types are not checked (G_DISABLE_CAST_CHECKS): the fakes are no real connections.
 */
#pragma once

#include <gio/gio.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static struct {
	GDBusConnection *bus;
	GHashTable *replies;       /* "Method" or "Method arg0" -> reply text */
	gboolean no_bus;           /* g_bus_get_sync fails, g_bus_own_name loses the name */
	gboolean register_fails;
	gboolean emit_fails;       /* g_dbus_connection_emit_signal fails (connection closed) */
	GPtrArray *subscriptions;  /* SgSub */
	GPtrArray *objects;        /* SgObject */
	GPtrArray *watches;        /* SgWatch */
	guint next_id;
	/* g_bus_own_name's handlers */
	GBusAcquiredCallback acquired;
	GBusNameLostCallback lost;
	gpointer own_data;
	char *own_name;
	int async_pending;
} sg;

typedef struct {
	guint id;
	char *member;
	GDBusSignalCallback cb;
	gpointer data;
} SgSub;

typedef struct {
	guint id;
	char *path;
	char *iface;
	GDBusInterfaceVTable vtable;
	gpointer data;
} SgObject;

typedef struct {
	guint id;
	char *name;
	GBusNameVanishedCallback vanished;
	gpointer data;
	gboolean active;
} SgWatch;

static void sg_init(void)
{
	if (sg.bus)
		return;
	sg.bus = (GDBusConnection *)g_object_new(G_TYPE_OBJECT, NULL);
	sg.replies = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, g_free);
	sg.subscriptions = g_ptr_array_new();
	sg.objects = g_ptr_array_new();
	sg.watches = g_ptr_array_new();
	sg.next_id = 100;
}

/* The reply to a method ("Method", or "Method firstarg" for one with that first string argument) */
static void sg_reply(const char *key, const char *text)
{
	sg_init();
	if (text)
		g_hash_table_replace(sg.replies, g_strdup(key), g_strdup(text));
	else
		g_hash_table_remove(sg.replies, key);
}

static char *sg_print(GVariant *v)
{
	return v ? g_variant_print(v, FALSE) : g_strdup("()");
}

static GVariant *sg_answer(const char *method, GVariant *params, const GVariantType *type, GError **error)
{
	const char *text = NULL;
	sg_init();
	if (params && g_variant_n_children(params) > 0) {
		g_autoptr(GVariant) first = g_variant_get_child_value(params, 0);
		if (g_variant_is_of_type(first, G_VARIANT_TYPE_STRING)) {
			g_autofree char *key = g_strdup_printf("%s %s", method, g_variant_get_string(first, NULL));
			text = g_hash_table_lookup(sg.replies, key);
		}
	}
	if (!text)
		text = g_hash_table_lookup(sg.replies, method);
	if (!text) {
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_SERVICE_UNKNOWN, "fake bus: no reply for %s", method);
		return NULL;
	}
	if (text[0] == '!') {
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_FAILED, "%s", text + 1);
		return NULL;
	}
	/* Not floating */
	return g_variant_parse(type, text, NULL, NULL, error);
}

static GVariant *sg_call_sync(GDBusConnection *bus, const char *dest, const char *path, const char *iface,
			      const char *method, GVariant *params, const GVariantType *type, GDBusCallFlags flags,
			      int timeout, GCancellable *cancel, GError **error)
{
	g_autoptr(GVariant) p = params ? g_variant_ref_sink(params) : NULL;
	g_autofree char *text = sg_print(p);
	printf("call-sync %s %s %s %s%s\n", dest, path, iface, method, text);
	return sg_answer(method, p, type, error);
}

static void sg_call(GDBusConnection *bus, const char *dest, const char *path, const char *iface,
		    const char *method, GVariant *params, const GVariantType *type, GDBusCallFlags flags,
		    int timeout, GCancellable *cancel, GAsyncReadyCallback cb, gpointer data)
{
	g_autoptr(GVariant) p = params ? g_variant_ref_sink(params) : NULL;
	g_autofree char *text = sg_print(p);
	printf("call %s %s %s %s%s\n", dest, path, iface, method, text);
	GTask *task = g_task_new(NULL, NULL, cb, data);
	GError *error = NULL;
	GVariant *reply = sg_answer(method, p, type, &error);
	sg.async_pending++;
	if (reply)
		g_task_return_pointer(task, reply, (GDestroyNotify)g_variant_unref);
	else
		g_task_return_error(task, error);
	g_object_unref(task);
}

static GVariant *sg_call_finish(GDBusConnection *bus, GAsyncResult *res, GError **error)
{
	sg.async_pending--;
	return g_task_propagate_pointer(G_TASK(res), error);
}

static gboolean sg_emit_signal(GDBusConnection *bus, const char *dest, const char *path, const char *iface,
			       const char *name, GVariant *params, GError **error)
{
	g_autoptr(GVariant) p = params ? g_variant_ref_sink(params) : NULL;
	g_autofree char *text = sg_print(p);
	printf("signal %s %s %s %s%s\n", dest ? dest : "*", path, iface, name, text);
	if (sg.emit_fails) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_CLOSED, "fake bus: connection closed");
		return FALSE;
	}
	return TRUE;
}

static guint sg_signal_subscribe(GDBusConnection *bus, const char *sender, const char *iface, const char *member,
				 const char *path, const char *arg0, GDBusSignalFlags flags, GDBusSignalCallback cb,
				 gpointer data, GDestroyNotify destroy)
{
	sg_init();
	SgSub *s = g_new0(SgSub, 1);
	s->id = ++sg.next_id;
	s->member = g_strdup(member);
	s->cb = cb;
	s->data = data;
	g_ptr_array_add(sg.subscriptions, s);
	printf("subscribe %s %s %s %s\n", sender, path, iface, member);
	return s->id;
}

/* A signal from the bus to every subscriber of that member */
static void sg_deliver(const char *member, const char *params)
{
	g_autoptr(GVariant) p = g_variant_parse(NULL, params, NULL, NULL, NULL);
	g_assert(p);
	for (guint i = 0; i < sg.subscriptions->len; i++) {
		SgSub *s = sg.subscriptions->pdata[i];
		if (!strcmp(s->member, member))
			s->cb(sg.bus, ":1.7", "/", "iface", member, p, s->data);
	}
}

static GDBusConnection *sg_bus_get_sync(GBusType type, GCancellable *cancel, GError **error)
{
	sg_init();
	if (sg.no_bus) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND, "fake bus: no system bus");
		return NULL;
	}
	return sg.bus;
}

static gboolean sg_own_idle(gpointer data)
{
	if (sg.no_bus)
		sg.lost(NULL, sg.own_name, sg.own_data);
	else if (sg.acquired)
		sg.acquired(sg.bus, sg.own_name, sg.own_data);
	return G_SOURCE_REMOVE;
}

/* As GDBus does: the handlers run from the main loop */
static guint sg_own_name(GBusType type, const char *name, GBusNameOwnerFlags flags, GBusAcquiredCallback acquired,
			 GBusNameAcquiredCallback name_acquired, GBusNameLostCallback lost, gpointer data,
			 GDestroyNotify destroy)
{
	sg_init();
	printf("own-name %s %s\n", type == G_BUS_TYPE_SYSTEM ? "system" : "session", name);
	sg.acquired = acquired;
	sg.lost = lost;
	sg.own_data = data;
	g_free(sg.own_name);
	sg.own_name = g_strdup(name);
	g_idle_add(sg_own_idle, NULL);
	return 7;
}

static void sg_unown_name(guint id)
{
	printf("unown-name %u\n", id);
}

/* The bus took the name away */
static void sg_lose_name(void)
{
	sg.lost(sg.bus, sg.own_name, sg.own_data);
}

static guint sg_register_object(GDBusConnection *bus, const char *path, GDBusInterfaceInfo *info,
				const GDBusInterfaceVTable *vtable, gpointer data, GDestroyNotify destroy,
				GError **error)
{
	sg_init();
	printf("register %s %s\n", path, info->name);
	if (sg.register_fails) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_EXISTS, "fake bus: object exists");
		return 0;
	}
	SgObject *o = g_new0(SgObject, 1);
	o->id = ++sg.next_id;
	o->path = g_strdup(path);
	o->iface = g_strdup(info->name);
	o->vtable = *vtable;
	o->data = data;
	g_ptr_array_add(sg.objects, o);
	return o->id;
}

static SgObject *sg_object(const char *path, const char *iface)
{
	for (guint i = 0; i < sg.objects->len; i++) {
		SgObject *o = sg.objects->pdata[i];
		if (!strcmp(o->path, path) && !strcmp(o->iface, iface))
			return o;
	}
	g_error("fake bus: nothing registered at %s %s", path, iface);
	return NULL;
}

/* ---- Method invocations: the answer is printed ---- */

typedef struct {
	char *sender;
	char *method;
} SgInv;

static void sg_inv_done(GDBusMethodInvocation *i)
{
	SgInv *inv = (SgInv *)i;
	g_free(inv->sender);
	g_free(inv->method);
	g_free(inv);
}

static void sg_return_value(GDBusMethodInvocation *i, GVariant *value)
{
	g_autoptr(GVariant) v = value ? g_variant_ref_sink(value) : NULL;
	g_autofree char *text = sg_print(v);
	printf("reply %s ok %s\n", ((SgInv *)i)->method, text);
	sg_inv_done(i);
}

static void sg_return_dbus_error(GDBusMethodInvocation *i, const char *name, const char *message)
{
	printf("reply %s error %s: %s\n", ((SgInv *)i)->method, name, message);
	sg_inv_done(i);
}

static void G_GNUC_PRINTF(4, 5) sg_return_error(GDBusMethodInvocation *i, GQuark domain, int code,
						 const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	g_autofree char *message = g_strdup_vprintf(fmt, ap);
	va_end(ap);
	g_autoptr(GError) e = g_error_new_literal(domain, code, message);
	g_autofree char *name = g_dbus_error_encode_gerror(e);
	sg_return_dbus_error(i, name, message);
}

#define sg_inv_sender(i) (((SgInv *)(i))->sender)

/* A method call from sender on the registered object */
static void sg_invoke(const char *path, const char *iface, const char *sender, const char *method,
		      const char *params)
{
	SgObject *o = sg_object(path, iface);
	SgInv *inv = g_new0(SgInv, 1);
	g_autoptr(GVariant) p = params ? g_variant_parse(NULL, params, NULL, NULL, NULL)
				       : g_variant_ref_sink(g_variant_new("()"));
	inv->sender = g_strdup(sender);
	inv->method = g_strdup(method);
	o->vtable.method_call(sg.bus, sender, path, iface, method, p, (GDBusMethodInvocation *)inv, o->data);
}

/* "get NAME=VALUE" or "get NAME error MESSAGE" */
static void sg_get(const char *path, const char *iface, const char *property)
{
	SgObject *o = sg_object(path, iface);
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) v = o->vtable.get_property(sg.bus, ":1.9", path, iface, property, &error, o->data);
	if (v) {
		g_autofree char *text = g_variant_print(g_variant_ref_sink(v), FALSE);
		printf("get %s=%s\n", property, text);
	} else {
		printf("get %s error %s\n", property, error ? error->message : "(none)");
	}
}

static void sg_set(const char *path, const char *iface, const char *property, const char *value)
{
	SgObject *o = sg_object(path, iface);
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) v = g_variant_parse(NULL, value, NULL, NULL, NULL);
	if (!o->vtable.set_property) {
		printf("set %s read-only interface\n", property);
		return;
	}
	gboolean ok = o->vtable.set_property(sg.bus, ":1.9", path, iface, property, v, &error, o->data);
	printf("set %s=%d%s%s\n", property, ok, error ? " " : "", error ? error->message : "");
}

/* ---- Name watches ---- */

static guint sg_watch_name(GDBusConnection *bus, const char *name, GBusNameWatcherFlags flags,
			   GBusNameAppearedCallback appeared, GBusNameVanishedCallback vanished, gpointer data,
			   GDestroyNotify destroy)
{
	sg_init();
	SgWatch *w = g_new0(SgWatch, 1);
	w->id = ++sg.next_id;
	w->name = g_strdup(name);
	w->vanished = vanished;
	w->data = data;
	w->active = TRUE;
	g_ptr_array_add(sg.watches, w);
	printf("watch %s\n", name);
	return w->id;
}

static void sg_unwatch_name(guint id)
{
	for (guint i = 0; i < sg.watches->len; i++) {
		SgWatch *w = sg.watches->pdata[i];
		if (w->id == id && w->active) {
			w->active = FALSE;
			printf("unwatch %s\n", w->name);
			return;
		}
	}
	printf("unwatch unknown %u\n", id);
}

/* The client left the bus: every active watch on its name fires */
static void sg_vanish(const char *name)
{
	for (guint i = 0; i < sg.watches->len; i++) {
		SgWatch *w = sg.watches->pdata[i];
		if (w->active && !strcmp(w->name, name))
			w->vanished(sg.bus, name, w->data);
	}
}

static int sg_active_watches(void)
{
	int n = 0;
	for (guint i = 0; sg.watches && i < sg.watches->len; i++)
		n += ((SgWatch *)sg.watches->pdata[i])->active;
	return n;
}

/* ---- Proxies ---- */

#define SG_PROXY_MAGIC 0x5350524fu

typedef struct {
	guint32 magic;
	char *iface;
	char *owner;               /* NULL: the service is not running */
	GHashTable *props;         /* name -> GVariant */
	GCallback on_props;
	gpointer props_data;
	GCallback on_signal;
	gpointer signal_data;
	GCallback on_owner;
	gpointer owner_data;
} SgProxy;

static GPtrArray *sg_proxies;
static char *sg_proxy_fails;   /* interface whose proxy cannot be made */

static SgProxy *sg_proxy(const char *iface)
{
	for (guint i = 0; sg_proxies && i < sg_proxies->len; i++) {
		SgProxy *p = sg_proxies->pdata[i];
		if (!strcmp(p->iface, iface))
			return p;
	}
	return NULL;
}

static GDBusProxy *sg_proxy_new_sync(GDBusConnection *bus, GDBusProxyFlags flags, GDBusInterfaceInfo *info,
				     const char *name, const char *path, const char *iface, GCancellable *cancel,
				     GError **error)
{
	printf("proxy %s %s %s\n", name, path, iface);
	if (sg_proxy_fails && !strcmp(sg_proxy_fails, iface)) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_FAILED, "fake bus: no proxy for %s", iface);
		return NULL;
	}
	SgProxy *p = sg_proxy(iface);
	if (!p) {
		p = g_new0(SgProxy, 1);
		p->magic = SG_PROXY_MAGIC;
		p->iface = g_strdup(iface);
		p->owner = g_strdup(":1.50");
		p->props = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, (GDestroyNotify)g_variant_unref);
		if (!sg_proxies)
			sg_proxies = g_ptr_array_new();
		g_ptr_array_add(sg_proxies, p);
	}
	return (GDBusProxy *)p;
}

/* A property of the service behind a proxy (made before the daemon asks for it) */
static void sg_proxy_prop(const char *iface, const char *name, const char *value)
{
	SgProxy *p = sg_proxy(iface);
	if (!p) {
		sg_proxy_new_sync(NULL, 0, NULL, "-", "-", iface, NULL, NULL);
		p = sg_proxy(iface);
	}
	if (value)
		g_hash_table_replace(p->props, g_strdup(name),
				     g_variant_parse(NULL, value, NULL, NULL, NULL));
	else
		g_hash_table_remove(p->props, name);
}

static GVariant *sg_proxy_get_cached_property(GDBusProxy *proxy, const char *name)
{
	SgProxy *p = (SgProxy *)proxy;
	GVariant *v = g_hash_table_lookup(p->props, name);
	return v ? g_variant_ref(v) : NULL;
}

static char *sg_proxy_get_name_owner(GDBusProxy *proxy)
{
	SgProxy *p = (SgProxy *)proxy;
	return g_strdup(p->owner);
}

static GVariant *sg_proxy_call_sync(GDBusProxy *proxy, const char *method, GVariant *params, GDBusCallFlags flags,
				    int timeout, GCancellable *cancel, GError **error)
{
	SgProxy *p = (SgProxy *)proxy;
	g_autoptr(GVariant) ps = params ? g_variant_ref_sink(params) : NULL;
	printf("proxy-call-sync %s %s\n", p->iface, method);
	return sg_answer(method, ps, NULL, error);
}

static void sg_proxy_call(GDBusProxy *proxy, const char *method, GVariant *params, GDBusCallFlags flags,
			  int timeout, GCancellable *cancel, GAsyncReadyCallback cb, gpointer data)
{
	SgProxy *p = (SgProxy *)proxy;
	if (params)
		g_variant_unref(g_variant_ref_sink(params));
	printf("proxy-call %s %s\n", p->iface, method);
}

static gulong sg_signal_connect(gpointer instance, const char *signal, GCallback cb, gpointer data)
{
	SgProxy *p = instance;
	if (p->magic != SG_PROXY_MAGIC)
		return g_signal_connect_data(instance, signal, cb, data, NULL, 0);
	if (!strcmp(signal, "g-properties-changed")) {
		p->on_props = cb;
		p->props_data = data;
	} else if (!strcmp(signal, "g-signal")) {
		p->on_signal = cb;
		p->signal_data = data;
	} else if (!strcmp(signal, "notify::g-name-owner")) {
		p->on_owner = cb;
		p->owner_data = data;
	} else {
		g_error("fake proxy: signal %s", signal);
	}
	return 1;
}

/* The service changed properties: cached, then g-properties-changed */
static void sg_proxy_changed(const char *iface, const char *name, const char *value)
{
	SgProxy *p = sg_proxy(iface);
	sg_proxy_prop(iface, name, value);
	GVariantBuilder b;
	g_variant_builder_init(&b, G_VARIANT_TYPE("a{sv}"));
	g_variant_builder_add(&b, "{sv}", name, g_hash_table_lookup(p->props, name));
	g_autoptr(GVariant) changed = g_variant_ref_sink(g_variant_builder_end(&b));
	const char *const invalidated[] = {NULL};
	if (p->on_props)
		((void (*)(GDBusProxy *, GVariant *, const char *const *, gpointer))p->on_props)(
			(GDBusProxy *)p, changed, invalidated, p->props_data);
}

static void sg_proxy_signal(const char *iface, const char *signal)
{
	SgProxy *p = sg_proxy(iface);
	g_autoptr(GVariant) params = g_variant_ref_sink(g_variant_new("()"));
	if (p->on_signal)
		((void (*)(GDBusProxy *, const char *, const char *, GVariant *, gpointer))p->on_signal)(
			(GDBusProxy *)p, ":1.50", signal, params, p->signal_data);
}

/* The service behind the proxy (re)started (owner) or went away (NULL) */
static void sg_proxy_owner(const char *iface, const char *owner)
{
	SgProxy *p = sg_proxy(iface);
	g_free(p->owner);
	p->owner = g_strdup(owner);
	if (p->on_owner)
		((void (*)(GObject *, GParamSpec *, gpointer))p->on_owner)((GObject *)p, NULL, p->owner_data);
}

#define g_bus_get_sync sg_bus_get_sync
#define g_bus_own_name sg_own_name
#define g_bus_unown_name sg_unown_name
#define g_bus_watch_name_on_connection sg_watch_name
#define g_bus_unwatch_name sg_unwatch_name
#define g_dbus_connection_call_sync sg_call_sync
#define g_dbus_connection_call sg_call
#define g_dbus_connection_call_finish sg_call_finish
#define g_dbus_connection_emit_signal sg_emit_signal
#define g_dbus_connection_signal_subscribe sg_signal_subscribe
#define g_dbus_connection_register_object sg_register_object
#define g_dbus_method_invocation_return_value sg_return_value
#define g_dbus_method_invocation_return_dbus_error sg_return_dbus_error
#define g_dbus_method_invocation_return_error sg_return_error
#define g_dbus_method_invocation_get_sender sg_inv_sender
#define g_dbus_proxy_new_sync sg_proxy_new_sync
#define g_dbus_proxy_get_cached_property sg_proxy_get_cached_property
#define g_dbus_proxy_get_name_owner sg_proxy_get_name_owner
#define g_dbus_proxy_call_sync sg_proxy_call_sync
#define g_dbus_proxy_call sg_proxy_call
#undef g_signal_connect
#define g_signal_connect(instance, signal, cb, data) sg_signal_connect((instance), (signal), G_CALLBACK(cb), (data))
