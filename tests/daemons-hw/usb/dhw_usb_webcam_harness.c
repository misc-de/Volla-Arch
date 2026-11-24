/*
 * Harness for plinius-usb-webcam.c (its main() renamed): which pipelines it runs for what
 * plinius-usb and the computer want, retrying, and ending when uvcsink keeps the node open.
 *
 * Faked: gst_parse_launch prints the description it gets ("pipeline DESCRIPTION") and builds a
 * stand-in, "identity ! dhwsink": dhwsink is a sink of this harness with uvcsink's "streaming"
 * property (no camera, no UVC device is reached). stat()/fstat() see a file in the test's
 * directory whose text is "chardev MAJ:MIN" as that character device, and one fd ("hold") as the
 * node uvcsink keeps open. The retry timers run on a virtual clock ("advance S").
 * Command mode: the cached properties of plinius-usb come from "prop NAME VALUE".
 * Daemon mode (argv[1] "daemon"): the program's main() against dhw_usb_fakeservice.h on the
 * private bus of the test; commands on stdin.
 */
#define _GNU_SOURCE
#include <gio/gio.h>
#include <glib-unix.h>
#include <gst/base/gstbasesink.h>
#include <gst/gst.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

#include "dhw_usb_fakeservice.h"

static gboolean daemon_mode;

/* ---- dhwsink: what the test sees of uvcsink ---- */

typedef struct {
	GstBaseSink parent;
	gboolean streaming;
	gboolean fail_start;
} DhwSink;

typedef struct {
	GstBaseSinkClass parent_class;
} DhwSinkClass;

G_DEFINE_TYPE(DhwSink, dhw_sink, GST_TYPE_BASE_SINK)

enum { PROP_0, PROP_STREAMING, PROP_FAIL_START };

static void dhw_sink_set_property(GObject *object, guint id, const GValue *value, GParamSpec *pspec)
{
	DhwSink *self = (DhwSink *)object;

	if (id == PROP_STREAMING)
		self->streaming = g_value_get_boolean(value);
	else
		self->fail_start = g_value_get_boolean(value);
}

static void dhw_sink_get_property(GObject *object, guint id, GValue *value, GParamSpec *pspec)
{
	DhwSink *self = (DhwSink *)object;

	g_value_set_boolean(value, id == PROP_STREAMING ? self->streaming : self->fail_start);
}

static gboolean dhw_sink_start(GstBaseSink *sink)
{
	return !((DhwSink *)sink)->fail_start;
}

static void dhw_sink_class_init(DhwSinkClass *klass)
{
	GObjectClass *object_class = G_OBJECT_CLASS(klass);
	static GstStaticPadTemplate sink_template =
		GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);

	object_class->set_property = dhw_sink_set_property;
	object_class->get_property = dhw_sink_get_property;
	g_object_class_install_property(object_class, PROP_STREAMING,
					g_param_spec_boolean("streaming", "", "", FALSE, G_PARAM_READWRITE));
	g_object_class_install_property(object_class, PROP_FAIL_START,
					g_param_spec_boolean("fail-start", "", "", FALSE, G_PARAM_READWRITE));
	gst_element_class_add_static_pad_template(GST_ELEMENT_CLASS(klass), &sink_template);
	gst_element_class_set_static_metadata(GST_ELEMENT_CLASS(klass), "dhwsink", "Sink", "test", "test");
	GST_BASE_SINK_CLASS(klass)->start = dhw_sink_start;
}

static void dhw_sink_init(DhwSink *self)
{
}

/* ---- Pipelines ---- */

static char fail_next[2][16]; /* [0] out, [1] camera: "", "parse" or "start" */

static GstElement *fake_parse_launch(const char *description, GError **error)
{
	gboolean out = strstr(description, "uvcsink") != NULL;
	char *fail = fail_next[out ? 0 : 1];
	g_autofree char *standin = NULL;

	printf("pipeline %s\n", description);
	if (!strcmp(fail, "parse")) {
		fail[0] = '\0';
		g_set_error(error, GST_PARSE_ERROR, GST_PARSE_ERROR_NO_SUCH_ELEMENT, "no element \"%s\"",
			    out ? "uvcsink" : "pipewiresrc");
		return NULL;
	}
	standin = g_strdup_printf("identity ! dhwsink name=%s fail-start=%s", out ? "sink" : "camera",
				  !strcmp(fail, "start") ? "true" : "false");
	fail[0] = '\0';
	return gst_parse_launch(standin, error);
}

/* ---- Character devices and the fd uvcsink keeps ---- */

static int held_fd = -1;
static dev_t held_rdev;

static gboolean chardev_of(const char *path, dev_t *rdev)
{
	g_autofree char *text = NULL;
	unsigned int major, minor;

	if (!g_file_get_contents(path, &text, NULL, NULL) || sscanf(text, "chardev %u:%u", &major, &minor) != 2)
		return FALSE;
	*rdev = makedev(major, minor);
	return TRUE;
}

static int fake_stat(const char *path, struct stat *st)
{
	dev_t rdev;

	if (stat(path, st) < 0)
		return -1;
	if (chardev_of(path, &rdev)) {
		st->st_mode = S_IFCHR | 0660;
		st->st_rdev = rdev;
	}
	return 0;
}

static int fake_fstat(int fd, struct stat *st)
{
	if (fstat(fd, st) < 0)
		return -1;
	if (fd == held_fd) {
		st->st_mode = S_IFCHR | 0660;
		st->st_rdev = held_rdev;
	}
	return 0;
}

/* ---- Timers on a virtual clock (seconds) ---- */

static struct {
	guint id;
	guint due;
	GSourceFunc fn;
} timers[16];
static guint now_s, timer_last = 1u << 24;

static guint fake_timeout_add_seconds(guint s, GSourceFunc fn, gpointer data)
{
	for (guint i = 0; i < G_N_ELEMENTS(timers); i++)
		if (!timers[i].id) {
			timers[i].id = ++timer_last;
			timers[i].due = now_s + s;
			timers[i].fn = fn;
			return timers[i].id;
		}
	abort();
}

static gboolean fake_source_remove(guint id)
{
	for (guint i = 0; i < G_N_ELEMENTS(timers); i++)
		if (timers[i].id == id) {
			timers[i].id = 0;
			return TRUE;
		}
	return g_source_remove(id);
}

/* ---- plinius-usb's properties (command mode) ---- */

static int fake_proxy_object;
#define FAKE_PROXY ((GDBusProxy *)&fake_proxy_object)
static GHashTable *props;

static GVariant *fake_cached_property(GDBusProxy *proxy, const char *name)
{
	const char *value;

	if (proxy != FAKE_PROXY)
		return g_dbus_proxy_get_cached_property(proxy, name);
	value = g_hash_table_lookup(props, name);
	if (!value || !strcmp(value, "nil"))
		return NULL;
	if (!strcmp(value, "int"))
		return g_variant_ref_sink(g_variant_new_int32(7));
	return g_variant_ref_sink(g_variant_new_string(strcmp(value, "-") ? value : ""));
}

#define gst_parse_launch fake_parse_launch
#define stat(p, s) fake_stat(p, s)
#define fstat(f, s) fake_fstat(f, s)
#define g_timeout_add_seconds fake_timeout_add_seconds
#define g_source_remove fake_source_remove
#define g_dbus_proxy_get_cached_property fake_cached_property
#define main real_main
#include "plinius-usb-webcam.c"
#undef main
#undef gst_parse_launch
#undef stat
#undef fstat
#undef g_timeout_add_seconds
#undef g_source_remove
#undef g_dbus_proxy_get_cached_property

static void pump(void)
{
	/* Bus watches and idle callbacks of the main context; a moment for GStreamer's threads */
	for (int i = 0; i < 5; i++) {
		while (g_main_context_iteration(NULL, FALSE))
			;
		g_usleep(2000);
	}
}

static GstElement *element(const char *which)
{
	GstElement *pipeline = !strcmp(which, "out") ? self.out : self.camera;

	return pipeline ? gst_bin_get_by_name(GST_BIN(pipeline), !strcmp(which, "out") ? "sink" : "camera") : NULL;
}

static void print_state(void)
{
	guint retry = 0;

	for (guint i = 0; i < G_N_ELEMENTS(timers); i++)
		if (timers[i].id)
			retry = timers[i].due - now_s;
	printf("state device=%s out=%d camera=%s streaming=%d retry=%u retry_s=%u rdev=%s\n", self.device ?: "-",
	       self.out != NULL, self.camera ? self.camera_name : "-", self.streaming, retry, self.retry_s,
	       self.rdev ? "set" : "0");
}

static const char *arg(const char *line, const char *keyword)
{
	return g_str_has_prefix(line, keyword) ? line + strlen(keyword) : NULL;
}

static void command(const char *line)
{
	char a[4096] = "", b[4096] = "";
	const char *x;

	printf("> %s\n", line);
	if (svc_command(line)) {
		/* done by the service */
	} else if ((x = arg(line, "prop ")) && sscanf(x, "%4095s %4095s", a, b) == 2) {
		g_hash_table_insert(props, g_strdup(a), g_strdup(b));
	} else if (!strcmp(line, "proxy none")) {
		self.usb = NULL;
	} else if (!strcmp(line, "update")) {
		update();
	} else if (!strcmp(line, "pump")) {
		pump();
	} else if (!strcmp(line, "state")) {
		print_state();
	} else if ((x = arg(line, "fail ")) && sscanf(x, "%4095s %4095s", a, b) == 2) {
		snprintf(fail_next[!strcmp(a, "out") ? 0 : 1], sizeof fail_next[0], "%s", b);
	} else if ((x = arg(line, "streaming ")) && sscanf(x, "%4095s", a) == 1) {
		g_autoptr(GstElement) sink = element("out");

		g_object_set(sink, "streaming", !strcmp(a, "1"), NULL);
		pump();
	} else if (!strcmp(line, "idle")) {
		on_streaming_changed_idle(NULL);
	} else if ((x = arg(line, "message ")) && sscanf(x, "%4095s %4095s", a, b) == 2) {
		/* "message out|camera error|eos|info": what the pipeline's bus gets */
		g_autoptr(GstElement) sink = element(a);
		g_autoptr(GError) error = g_error_new_literal(GST_RESOURCE_ERROR, GST_RESOURCE_ERROR_BUSY,
							      "Device is busy");
		GstElement *pipeline = !strcmp(a, "out") ? self.out : self.camera;

		if (!strcmp(b, "eos")) {
			/* The pipeline's own EOS, once all its sinks ended */
			g_autoptr(GstBus) bus = gst_element_get_bus(pipeline);

			gst_bus_post(bus, gst_message_new_eos(GST_OBJECT(pipeline)));
		} else {
			gst_element_post_message(sink, !strcmp(b, "error")
							       ? gst_message_new_error(GST_OBJECT(sink), error, "dhw debug")
							       : gst_message_new_info(GST_OBJECT(sink), error, NULL));
		}
		pump();
	} else if ((x = arg(line, "message-nodebug ")) && sscanf(x, "%4095s", a) == 1) {
		g_autoptr(GstElement) sink = element(a);
		g_autoptr(GError) error = g_error_new_literal(GST_RESOURCE_ERROR, GST_RESOURCE_ERROR_BUSY, "Gone");

		gst_element_post_message(sink, gst_message_new_error(GST_OBJECT(sink), error, NULL));
		pump();
	} else if ((x = arg(line, "hold ")) && sscanf(x, "%4095s", a) == 1) {
		/* uvcsink's dup of the node: an fd the harness has, seen as the device */
		if (!chardev_of(a, &held_rdev))
			abort();
		held_fd = dup(1);
	} else if ((x = arg(line, "advance ")) && sscanf(x, "%4095s", a) == 1) {
		guint end = now_s + (guint)atoi(a);

		for (;;) {
			int best = -1;

			for (guint i = 0; i < G_N_ELEMENTS(timers); i++)
				if (timers[i].id && timers[i].due <= end && (best < 0 || timers[i].due < timers[best].due))
					best = (int)i;
			if (best < 0)
				break;
			now_s = timers[best].due;
			{
				guint id = timers[best].id;
				GSourceFunc fn = timers[best].fn;
				gboolean again = fn(NULL);

				if (timers[best].id == id && !again)
					timers[best].id = 0;
			}
		}
		now_s = end;
	} else if ((x = arg(line, "node ")) && sscanf(x, "%4095s", a) == 1) {
		/* The node shows up */
		if (!g_file_set_contents(a, "chardev 81:3\n", -1, NULL))
			abort();
	} else if (!strcmp(line, "quit")) {
		raise(SIGTERM);
	} else if (*line) {
		printf("<unknown command: %.60s>\n", line);
	}
	if (daemon_mode)
		puts("done");
}

static gboolean on_stdin(GIOChannel *channel, GIOCondition condition, gpointer user_data)
{
	g_autofree char *line = NULL;

	if (g_io_channel_read_line(channel, &line, NULL, NULL, NULL) != G_IO_STATUS_NORMAL)
		return G_SOURCE_REMOVE;
	g_strchomp(line);
	command(line);
	return G_SOURCE_CONTINUE;
}

int main(int argc, char **argv)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	gst_init(NULL, NULL);
	gst_element_register(NULL, "dhwsink", GST_RANK_NONE, dhw_sink_get_type());
	props = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, g_free);
	if (argc > 1 && !strcmp(argv[1], "daemon")) {
		GIOChannel *in = g_io_channel_unix_new(0);
		char *args[] = {argv[0], NULL};
		int r;

		daemon_mode = TRUE;
		if (argc > 2 && strcmp(argv[2], "-"))
			svc_start(argv + 2);
		g_io_add_watch(in, G_IO_IN | G_IO_HUP, on_stdin, NULL);
		r = real_main(1, args);
		svc_settle();
		printf("exit %d\n", r);
		return r;
	}
	self.usb = FAKE_PROXY;
	for (int i = 1; i < argc; i++)
		command(argv[i]);
	return 0;
}
