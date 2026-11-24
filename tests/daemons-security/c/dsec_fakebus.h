/*
 * dsec_fakebus.h: a scripted system bus for daemons under test. Included after gio.h and before
 * the daemon's .c file, it replaces the GDBus calls the daemons make for permission decisions:
 *
 *   GetConnectionUnixUser               -> fb.caller (-1: the bus does not know the sender)
 *   GetConnectionCredentials            -> UnixUserID fb.caller, UnixGroupIDs fb.groups
 *                                          (comma-separated, "" an empty list, "-" no such key)
 *   getgrnam ("wheel")                  -> gid fb.wheel (-1: no such group); not the host's
 *   login1 Seat ActiveSession           -> fb.session ("" no answer, "badtype" wrong type, "/" none)
 *   login1 Session User / LockedHint / Class -> fb.session_uid, fb.locked, fb.session_class
 *                                          (-1 no answer, -2 wrong type)
 *   g_dbus_connection_call (async)      -> recorded in fb.async_calls ("DEST METHOD")
 *   method invocations                  -> FakeInv: the answer lands in its fields
 *
 * fb_command() takes "bus KEY VALUE" lines of a harness's command loop.
 */
#pragma once

#include <gio/gio.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <grp.h>

static struct {
    gint64 caller;
    char groups[128];
    gint64 wheel;
    char session[128];
    gint64 session_uid;
    int locked;
    char session_class[32];
    int sync_calls;
    GString *async_calls;
    int signals;
} fb = { 1001, "1001,998", 998, "/org/freedesktop/login1/session/_31", 1001, 0, "user", 0, NULL, 0 };

/* The group database as the daemons ask it: only wheel exists, with id fb.wheel */
static struct group *
fake_getgrnam (const char *name)
{
    static struct group g;
    static char *no_members[] = { NULL };

    if (fb.wheel < 0 || strcmp (name, "wheel") != 0)
        return NULL;
    g = (struct group) { .gr_name = (char *) "wheel", .gr_passwd = (char *) "x",
                         .gr_gid = (gid_t) fb.wheel, .gr_mem = no_members };
    return &g;
}

static GVariant *
fb_reply (GVariant *value)
{
    return g_variant_ref_sink (value);
}

static GVariant *
fake_call_sync (GDBusConnection *bus, const char *dest, const char *path, const char *iface,
                const char *method, GVariant *params, const GVariantType *reply_type,
                GDBusCallFlags flags, int timeout, GCancellable *cancellable, GError **error)
{
    g_autoptr (GVariant) p = params ? g_variant_ref_sink (params) : NULL;

    (void) bus;
    (void) dest;
    (void) iface;
    (void) reply_type;
    (void) flags;
    (void) timeout;
    (void) cancellable;
    fb.sync_calls++;
    if (g_strcmp0 (method, "GetConnectionUnixUser") == 0) {
        if (fb.caller < 0) {
            g_set_error (error, G_DBUS_ERROR, G_DBUS_ERROR_NAME_HAS_NO_OWNER, "fake bus: no such sender");
            return NULL;
        }
        return fb_reply (g_variant_new ("(u)", (guint32) fb.caller));
    }
    if (g_strcmp0 (method, "GetConnectionCredentials") == 0) {
        GVariantBuilder creds;

        if (fb.caller < 0) {
            g_set_error (error, G_DBUS_ERROR, G_DBUS_ERROR_NAME_HAS_NO_OWNER, "fake bus: no such sender");
            return NULL;
        }
        g_variant_builder_init (&creds, G_VARIANT_TYPE ("a{sv}"));
        g_variant_builder_add (&creds, "{sv}", "UnixUserID", g_variant_new_uint32 ((guint32) fb.caller));
        g_variant_builder_add (&creds, "{sv}", "ProcessID", g_variant_new_uint32 (4242));
        if (strcmp (fb.groups, "-") != 0) {
            g_auto (GStrv) ids = g_strsplit (fb.groups, ",", -1);
            GVariantBuilder gids;

            g_variant_builder_init (&gids, G_VARIANT_TYPE ("au"));
            for (char **id = ids; *id; id++)
                if (**id)
                    g_variant_builder_add (&gids, "u", (guint32) g_ascii_strtoull (*id, NULL, 10));
            g_variant_builder_add (&creds, "{sv}", "UnixGroupIDs", g_variant_builder_end (&gids));
        }
        return fb_reply (g_variant_new ("(a{sv})", &creds));
    }
    if (g_strcmp0 (method, "Get") == 0 && p && g_variant_is_of_type (p, G_VARIANT_TYPE ("(ss)"))) {
        const char *ifc, *prop;

        g_variant_get (p, "(&s&s)", &ifc, &prop);
        if (strcmp (prop, "ActiveSession") == 0) {
            if (!fb.session[0])
                goto none;
            if (strcmp (fb.session, "badtype") == 0)
                return fb_reply (g_variant_new ("(v)", g_variant_new_string ("x")));
            return fb_reply (g_variant_new ("(v)", g_variant_new ("(so)", "31", fb.session)));
        }
        if (strcmp (prop, "User") == 0 && strcmp (path, fb.session) == 0) {
            if (fb.session_uid == -1)
                goto none;
            if (fb.session_uid == -2)
                return fb_reply (g_variant_new ("(v)", g_variant_new_uint32 (1)));
            return fb_reply (g_variant_new ("(v)", g_variant_new ("(uo)", (guint32) fb.session_uid,
                                                                 "/org/freedesktop/login1/user/_1")));
        }
        if (strcmp (prop, "LockedHint") == 0 && strcmp (path, fb.session) == 0) {
            if (fb.locked == -1)
                goto none;
            if (fb.locked == -2)
                return fb_reply (g_variant_new ("(v)", g_variant_new_string ("no")));
            return fb_reply (g_variant_new ("(v)", g_variant_new_boolean (fb.locked)));
        }
        if (strcmp (prop, "Class") == 0)
            return fb_reply (g_variant_new ("(v)", g_variant_new_string (fb.session_class)));
    }
none:
    g_set_error (error, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_METHOD, "fake bus: %s %s", method, path);
    return NULL;
}

static void
fake_call (GDBusConnection *bus, const char *dest, const char *path, const char *iface,
           const char *method, GVariant *params, const GVariantType *reply_type,
           GDBusCallFlags flags, int timeout, GCancellable *cancellable,
           GAsyncReadyCallback callback, gpointer user_data)
{
    (void) bus;
    (void) path;
    (void) iface;
    (void) reply_type;
    (void) flags;
    (void) timeout;
    (void) cancellable;
    (void) callback;
    (void) user_data;
    if (params)
        g_variant_unref (g_variant_ref_sink (params));
    if (!fb.async_calls)
        fb.async_calls = g_string_new (NULL);
    g_string_append_printf (fb.async_calls, "%s%s %s", fb.async_calls->len ? "," : "", dest, method);
}

static gboolean
fake_emit_signal (GDBusConnection *bus, const char *dest, const char *path, const char *iface,
                  const char *signal, GVariant *params, GError **error)
{
    (void) bus;
    (void) dest;
    (void) path;
    (void) iface;
    (void) signal;
    (void) error;
    if (params)
        g_variant_unref (g_variant_ref_sink (params));
    fb.signals++;
    return TRUE;
}

/* ---- Method invocations ---- */

typedef struct {
    const char *method;
    GVariant *params;
    gboolean answered;
    char *error_name; /* NULL: a value came back */
    char *error_message;
    GVariant *value;
} FakeInv;

#define FAKE_INV(i) ((FakeInv *) (i))

static void
fake_inv_reset (FakeInv *inv, const char *method, GVariant *params)
{
    g_clear_pointer (&inv->error_name, g_free);
    g_clear_pointer (&inv->error_message, g_free);
    g_clear_pointer (&inv->value, g_variant_unref);
    g_clear_pointer (&inv->params, g_variant_unref);
    inv->method = method;
    inv->params = params ? g_variant_ref_sink (params) : NULL;
    inv->answered = FALSE;
}

static void
fake_return_value (GDBusMethodInvocation *i, GVariant *value)
{
    FAKE_INV (i)->answered = TRUE;
    FAKE_INV (i)->value = value ? g_variant_ref_sink (value) : NULL;
}

static void
fake_return_dbus_error (GDBusMethodInvocation *i, const char *name, const char *message)
{
    FAKE_INV (i)->answered = TRUE;
    FAKE_INV (i)->error_name = g_strdup (name);
    FAKE_INV (i)->error_message = g_strdup (message);
}

static void G_GNUC_PRINTF (4, 5)
fake_return_error (GDBusMethodInvocation *i, GQuark domain, int code, const char *fmt, ...)
{
    va_list args;

    va_start (args, fmt);
    FAKE_INV (i)->answered = TRUE;
    FAKE_INV (i)->error_name = g_strdup_printf ("%s:%d", g_quark_to_string (domain), code);
    FAKE_INV (i)->error_message = g_strdup_vprintf (fmt, args);
    va_end (args);
}

static void
fake_return_error_literal (GDBusMethodInvocation *i, GQuark domain, int code, const char *message)
{
    fake_return_error (i, domain, code, "%s", message);
}

static void
fake_return_gerror (GDBusMethodInvocation *i, const GError *error)
{
    fake_return_error (i, error->domain, error->code, "%s", error->message);
}

/* "ok" / "ok VALUE" / "error NAME: MESSAGE" / "pending" */
static void
fake_inv_print (const FakeInv *inv)
{
    if (!inv->answered)
        puts ("pending");
    else if (inv->error_name)
        printf ("error %s: %s\n", inv->error_name, inv->error_message);
    else if (inv->value) {
        g_autofree char *text = g_variant_print (inv->value, FALSE);

        printf ("ok %s\n", text);
    } else
        puts ("ok");
}

#define getgrnam fake_getgrnam
#define g_dbus_connection_call_sync fake_call_sync
#define g_dbus_connection_call fake_call
#define g_dbus_connection_emit_signal fake_emit_signal
#define g_dbus_method_invocation_get_method_name(i) (FAKE_INV (i)->method)
#define g_dbus_method_invocation_get_parameters(i) (FAKE_INV (i)->params)
#define g_dbus_method_invocation_return_value fake_return_value
#define g_dbus_method_invocation_return_dbus_error fake_return_dbus_error
#define g_dbus_method_invocation_return_error fake_return_error
#define g_dbus_method_invocation_return_error_literal fake_return_error_literal
#define g_dbus_method_invocation_return_gerror fake_return_gerror

/* "bus caller 1001", "bus groups 1001,998" (none: an empty list, -: no list), "bus wheel 998",
 * "bus session /path", "bus suid 1001", "bus locked 1", "bus class greeter" */
static gboolean
fb_command (const char *line)
{
    char key[32], value[128];

    if (sscanf (line, "bus %31s %127s", key, value) != 2)
        return FALSE;
    if (strcmp (key, "caller") == 0)
        fb.caller = g_ascii_strtoll (value, NULL, 10);
    else if (strcmp (key, "groups") == 0)
        snprintf (fb.groups, sizeof fb.groups, "%s", strcmp (value, "none") ? value : "");
    else if (strcmp (key, "wheel") == 0)
        fb.wheel = g_ascii_strtoll (value, NULL, 10);
    else if (strcmp (key, "session") == 0)
        snprintf (fb.session, sizeof fb.session, "%s", strcmp (value, "-") ? value : "");
    else if (strcmp (key, "suid") == 0)
        fb.session_uid = g_ascii_strtoll (value, NULL, 10);
    else if (strcmp (key, "locked") == 0)
        fb.locked = atoi (value);
    else if (strcmp (key, "class") == 0)
        snprintf (fb.session_class, sizeof fb.session_class, "%s", value);
    else
        return FALSE;
    puts ("ok");
    return TRUE;
}
