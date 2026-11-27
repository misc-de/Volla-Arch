/*
 * Host stand-in for libgbinder, for unit tests of the binder bridges (tests/daemons-hw).
 *
 * Nothing here opens /dev/binder. Readers take their values from a scripted list
 * (fake_gb_request / fake_gb_reply), writers and transactions append a line to fake_gb_log,
 * so a test can check what a daemon would have sent to the HAL.
 */
#ifndef FAKE_GBINDER_H
#define FAKE_GBINDER_H

#include <glib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>

struct fake_gb_obj {
	int unused;
};
typedef struct fake_gb_obj GBinderServiceManager;
typedef struct fake_gb_obj GBinderRemoteObject;
typedef struct fake_gb_obj GBinderClient;
typedef struct fake_gb_obj GBinderLocalObject;
typedef struct fake_gb_obj GBinderLocalRequest;
typedef struct fake_gb_obj GBinderLocalReply;
typedef struct fake_gb_obj GBinderRemoteReply;
typedef struct fake_gb_obj GBinderRemoteRequest;
typedef struct fake_gb_obj GBinderFmq;

/* One scripted value. kind: 'i' int32, 'u' uint32, 'l' int64, 'd' double, 'f' float,
 * 's' string16, 'n' null string16, '8' string8 */
typedef struct {
	char kind;
	gint64 i;
	double d;
	const char *s;
} FakeGbValue;

typedef struct {
	const FakeGbValue *v;
	int n;
	int pos;
} GBinderReader;

typedef struct {
	int unused;
} GBinderWriter;

typedef GBinderLocalReply *(*GBinderLocalTransactFunc)(GBinderLocalObject *, GBinderRemoteRequest *, guint, guint,
							int *, void *);
typedef void (*GBinderRemoteObjectNotifyFunc)(GBinderRemoteObject *, void *);
typedef void (*GBinderClientReplyFunc)(GBinderClient *, GBinderRemoteReply *, int, void *);

enum { GBINDER_STATUS_OK = 0, GBINDER_STATUS_FAILED = -1 };
enum { GBINDER_STABILITY_UNDECLARED = 0, GBINDER_STABILITY_VENDOR = 0x3, GBINDER_STABILITY_SYSTEM = 0xc,
       GBINDER_STABILITY_VINTF = 0x3f };
#define GBINDER_TX_FLAG_ONEWAY 0x01
#define GBINDER_FMQ_TYPE_SYNC_READ_WRITE 1
#define GBINDER_FMQ_FLAG_CONFIGURE_EVENT_FLAG 1

static struct fake_gb_obj fake_gb_instance;
static GString *fake_gb_log;
static const FakeGbValue *fake_gb_request_vals;
static int fake_gb_request_n;
static const FakeGbValue *fake_gb_reply_vals;
static int fake_gb_reply_n;
static const char *fake_gb_request_iface;
static int fake_gb_status;              /* status of synchronous transactions */
static gboolean fake_gb_no_service;     /* get_service_sync finds nothing */
static gboolean fake_gb_transact_fails; /* gbinder_client_transact returns 0 */
/* The reply handler of the last asynchronous transaction, for the test to call */
static GBinderClientReplyFunc fake_gb_reply_fn;
static void *fake_gb_reply_user;

static void fake_gb_logf(const char *fmt, ...) G_GNUC_PRINTF(1, 2);
static void fake_gb_logf(const char *fmt, ...)
{
	va_list ap;
	if (!fake_gb_log)
		fake_gb_log = g_string_new(NULL);
	va_start(ap, fmt);
	g_string_append_vprintf(fake_gb_log, fmt, ap);
	va_end(ap);
}

static const FakeGbValue *fake_gb_next(GBinderReader *r, char kind)
{
	if (!r || r->pos >= r->n || r->v[r->pos].kind != kind)
		return NULL;
	return &r->v[r->pos++];
}

/* ---- reader ---- */
static gboolean gbinder_reader_read_int32(GBinderReader *r, gint32 *out)
{
	const FakeGbValue *v = fake_gb_next(r, 'i');
	if (!v)
		return FALSE;
	if (out)
		*out = (gint32)v->i;
	return TRUE;
}
static gboolean gbinder_reader_read_uint32(GBinderReader *r, guint32 *out)
{
	const FakeGbValue *v = fake_gb_next(r, 'u');
	if (!v)
		v = fake_gb_next(r, 'i');
	if (!v)
		return FALSE;
	if (out)
		*out = (guint32)v->i;
	return TRUE;
}
static gboolean gbinder_reader_read_int64(GBinderReader *r, gint64 *out)
{
	const FakeGbValue *v = fake_gb_next(r, 'l');
	if (!v)
		return FALSE;
	if (out)
		*out = v->i;
	return TRUE;
}
static gboolean gbinder_reader_read_double(GBinderReader *r, double *out)
{
	const FakeGbValue *v = fake_gb_next(r, 'd');
	if (!v)
		return FALSE;
	if (out)
		*out = v->d;
	return TRUE;
}
static gboolean gbinder_reader_read_float(GBinderReader *r, float *out)
{
	const FakeGbValue *v = fake_gb_next(r, 'f');
	if (!v)
		return FALSE;
	if (out)
		*out = (float)v->d;
	return TRUE;
}
static char *gbinder_reader_read_string16(GBinderReader *r)
{
	const FakeGbValue *v = fake_gb_next(r, 's');
	if (v)
		return g_strdup(v->s);
	fake_gb_next(r, 'n');
	return NULL;
}
static gboolean gbinder_reader_skip_string16(GBinderReader *r)
{
	return fake_gb_next(r, 's') || fake_gb_next(r, 'n');
}
static const char *gbinder_reader_read_string8(GBinderReader *r)
{
	const FakeGbValue *v = fake_gb_next(r, '8');
	return v ? v->s : NULL;
}
static void gbinder_reader_copy(GBinderReader *dst, const GBinderReader *src)
{
	*dst = *src;
}
/* A file descriptor: scripted as 'h' with the number of an open fd, which is dup()ed */
static int gbinder_reader_read_dup_fd(GBinderReader *r)
{
	const FakeGbValue *v = fake_gb_next(r, 'h');
	return v ? dup((int)v->i) : -1;
}
/* The raw parcel: the fake has none, only scripted values; each counts as 4 bytes, like
 * gbinder_reader_bytes_remaining() */
static const void *gbinder_reader_get_data(const GBinderReader *r, gsize *size)
{
	static const guint8 fake_gb_data[4096];
	if (size)
		*size = r ? MIN((gsize)r->n * 4, sizeof(fake_gb_data)) : 0;
	return fake_gb_data;
}
static gsize gbinder_reader_bytes_remaining(const GBinderReader *r)
{
	return r ? (gsize)(r->n - r->pos) * 4 : 0;
}
/* A binder object: scripted as 'o' (present) */
static GBinderRemoteObject *gbinder_reader_read_object(GBinderReader *r)
{
	return fake_gb_next(r, 'o') ? &fake_gb_instance : NULL;
}

/* ---- requests and replies ---- */
static void gbinder_remote_request_init_reader(GBinderRemoteRequest *req, GBinderReader *r)
{
	r->v = fake_gb_request_vals;
	r->n = fake_gb_request_n;
	r->pos = 0;
}
static const char *gbinder_remote_request_interface(GBinderRemoteRequest *req)
{
	return fake_gb_request_iface;
}
static void gbinder_remote_reply_init_reader(GBinderRemoteReply *reply, GBinderReader *r)
{
	r->v = fake_gb_reply_vals;
	r->n = fake_gb_reply_n;
	r->pos = 0;
}
static void gbinder_remote_reply_unref(GBinderRemoteReply *reply) {}
static GBinderLocalReply *gbinder_local_object_new_reply(GBinderLocalObject *obj)
{
	return &fake_gb_instance;
}
static GBinderLocalReply *gbinder_local_reply_append_int32(GBinderLocalReply *reply, guint32 v)
{
	fake_gb_logf("reply-i32 %d\n", (gint32)v);
	return reply;
}
static void gbinder_local_reply_init_writer(GBinderLocalReply *reply, GBinderWriter *w) {}
static void gbinder_local_reply_unref(GBinderLocalReply *reply) {}
/* Requests answered later */
static GBinderRemoteRequest *gbinder_remote_request_ref(GBinderRemoteRequest *req)
{
	return req;
}
static void gbinder_remote_request_unref(GBinderRemoteRequest *req) {}
static void gbinder_remote_request_block(GBinderRemoteRequest *req)
{
	fake_gb_logf("block\n");
}
static void gbinder_remote_request_complete(GBinderRemoteRequest *req, GBinderLocalReply *reply, int status)
{
	fake_gb_logf("complete %d\n", status);
}

/* ---- writers ---- */
static GBinderLocalRequest *gbinder_client_new_request(GBinderClient *c)
{
	fake_gb_logf("request\n");
	return &fake_gb_instance;
}
static GBinderLocalRequest *gbinder_client_new_request2(GBinderClient *c, guint32 code)
{
	fake_gb_logf("request %u\n", code);
	return &fake_gb_instance;
}
static void gbinder_local_request_unref(GBinderLocalRequest *req) {}
static void gbinder_local_request_init_writer(GBinderLocalRequest *req, GBinderWriter *w) {}
static GBinderLocalRequest *gbinder_local_request_append_int32(GBinderLocalRequest *req, guint32 v)
{
	fake_gb_logf("i32 %d\n", (gint32)v);
	return req;
}
static GBinderLocalRequest *gbinder_local_request_append_int64(GBinderLocalRequest *req, guint64 v)
{
	fake_gb_logf("i64 %" G_GINT64_FORMAT "\n", (gint64)v);
	return req;
}
static GBinderLocalRequest *gbinder_local_request_append_local_object(GBinderLocalRequest *req,
								      GBinderLocalObject *o)
{
	fake_gb_logf("object\n");
	return req;
}
static void gbinder_writer_append_int32(GBinderWriter *w, guint32 v)
{
	fake_gb_logf("i32 %d\n", (gint32)v);
}
static void gbinder_writer_append_int64(GBinderWriter *w, guint64 v)
{
	fake_gb_logf("i64 %" G_GINT64_FORMAT "\n", (gint64)v);
}
static void gbinder_writer_start_parcelable(GBinderWriter *w, GBinderWriter *parcelable)
{
	fake_gb_logf("parcelable {\n");
}
static void gbinder_writer_finish_parcelable(GBinderWriter *parcelable)
{
	fake_gb_logf("}\n");
}
static void gbinder_writer_append_bytes(GBinderWriter *w, const void *data, gsize n)
{
	fake_gb_logf("bytes %zu\n", n);
}
static void gbinder_writer_append_float(GBinderWriter *w, float v)
{
	fake_gb_logf("float %.4f\n", v);
}
static void gbinder_writer_append_string16(GBinderWriter *w, const char *s)
{
	fake_gb_logf("s16 %s\n", s ? s : "(null)");
}
static void gbinder_writer_append_local_object(GBinderWriter *w, GBinderLocalObject *o)
{
	fake_gb_logf("object %s\n", o ? "set" : "null");
}
static void gbinder_writer_append_fmq_descriptor(GBinderWriter *w, GBinderFmq *q)
{
	fake_gb_logf("fmq\n");
}

/* ---- transactions ---- */
static GBinderRemoteReply *gbinder_client_transact_sync_reply(GBinderClient *c, guint32 code,
							      GBinderLocalRequest *req, int *status)
{
	fake_gb_logf("tx %u\n", code);
	if (status)
		*status = fake_gb_status;
	return &fake_gb_instance;
}
static gulong gbinder_client_transact(GBinderClient *c, guint32 code, guint32 flags, GBinderLocalRequest *req,
				      GBinderClientReplyFunc reply, GDestroyNotify destroy, void *user)
{
	fake_gb_logf("tx-async %u flags %u\n", code, flags);
	if (fake_gb_transact_fails)
		return 0;
	fake_gb_reply_fn = reply;
	fake_gb_reply_user = user;
	return 1;
}

/* ---- objects ---- */
static GBinderServiceManager *gbinder_servicemanager_new(const char *dev)
{
	fake_gb_logf("servicemanager %s\n", dev);
	return &fake_gb_instance;
}
static void gbinder_servicemanager_unref(GBinderServiceManager *sm) {}
static gboolean gbinder_servicemanager_wait(GBinderServiceManager *sm, long ms)
{
	return TRUE;
}
static GBinderRemoteObject *gbinder_servicemanager_get_service_sync(GBinderServiceManager *sm, const char *name,
								   int *status)
{
	fake_gb_logf("get-service %s\n", name);
	if (status)
		*status = 0;
	return fake_gb_no_service ? NULL : &fake_gb_instance;
}
static GBinderLocalObject *gbinder_servicemanager_new_local_object(GBinderServiceManager *sm, const char *iface,
								  GBinderLocalTransactFunc fn, void *user)
{
	return &fake_gb_instance;
}
typedef void (*GBinderServiceManagerFunc)(GBinderServiceManager *, void *);
typedef void (*GBinderServiceManagerAddServiceFunc)(GBinderServiceManager *, int, void *);
static gboolean fake_gb_sm_present = TRUE; /* the service manager is there */
static int fake_gb_add_status;             /* status add_service reports */
static gboolean gbinder_servicemanager_is_present(GBinderServiceManager *sm)
{
	return fake_gb_sm_present;
}
static gulong gbinder_servicemanager_add_service(GBinderServiceManager *sm, const char *name, GBinderLocalObject *o,
						 GBinderServiceManagerAddServiceFunc fn, void *user)
{
	fake_gb_logf("add-service %s\n", name);
	if (fn)
		fn(sm, fake_gb_add_status, user);
	return 1;
}
static gulong gbinder_servicemanager_add_presence_handler(GBinderServiceManager *sm, GBinderServiceManagerFunc fn,
							   void *user)
{
	return 1;
}
static void gbinder_local_object_set_stability(GBinderLocalObject *o, int stability) {}
static void gbinder_local_object_unref(GBinderLocalObject *o) {}
static GBinderRemoteObject *gbinder_remote_object_ref(GBinderRemoteObject *o)
{
	return o;
}
static void gbinder_remote_object_unref(GBinderRemoteObject *o) {}
static gulong gbinder_remote_object_add_death_handler(GBinderRemoteObject *o, GBinderRemoteObjectNotifyFunc fn,
						      void *user)
{
	return 1;
}
static void gbinder_remote_object_remove_handler(GBinderRemoteObject *o, gulong id) {}
static GBinderClient *gbinder_client_new(GBinderRemoteObject *o, const char *iface)
{
	return &fake_gb_instance;
}
static void gbinder_client_unref(GBinderClient *c) {}

/* ---- fast message queues: never used by the tests ---- */
static GBinderFmq *gbinder_fmq_new(gsize item, gsize n, int type, int flags, int fd, gsize size)
{
	return NULL;
}
static gsize gbinder_fmq_available_to_read(GBinderFmq *q)
{
	return 0;
}
static int gbinder_fmq_wait_timeout(GBinderFmq *q, guint32 bits, guint32 *state, int ms)
{
	return -1;
}
static gboolean gbinder_fmq_read(GBinderFmq *q, void *data, gsize n)
{
	return FALSE;
}
static gboolean gbinder_fmq_write(GBinderFmq *q, const void *data, gsize n)
{
	return FALSE;
}
static int gbinder_fmq_wake(GBinderFmq *q, guint32 bits)
{
	return 0;
}

#endif
