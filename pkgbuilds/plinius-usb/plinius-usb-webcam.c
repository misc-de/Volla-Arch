// SPDX-License-Identifier: MIT
// plinius-usb-webcam: streams a camera of the phone into the webcam function of plinius-usb, in
// the session of the user signed in (the cameras of camera-hal-bridge are in the user's PipeWire,
// the video node of the gadget is the user's through uaccess).
//
// plinius-usb tells the node (WebcamDevice, empty while there is no webcam) and the camera
// (WebcamCamera, a PipeWire node name). Two pipelines, joined by intervideosink/intervideosrc:
// - Out, as long as there is a node: intervideosrc → JPEG → uvcsink. It keeps the node open,
//   which f_uvc needs to put the gadget on the bus at all, and answers the computer's requests.
//   Without a picture from the camera, intervideosrc sends black.
// - Camera, only while the computer streams (uvcsink's "streaming"): pipewiresrc → videoflip.
//   The camera is not on otherwise, and switching cameras restarts only this one, so the
//   computer keeps its device.
//
// Picture: the bridge turns frames upright for portrait and tags how the phone is held; videoflip
// turns them by that tag, so a phone held sideways gives a landscape picture filling 16:9. Upright
// it has bars on both sides (square pixels, videoscale adds borders) rather than being stretched.
#include <gio/gio.h>
#include <glib-unix.h>
#include <gst/gst.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#define USB_NAME "org.plinius.Usb"
#define USB_PATH "/org/plinius/Usb"
#define USB_INTERFACE "org.plinius.Usb"

#define CHANNEL "plinius-usb-webcam"
/* Retry after a pipeline failed, e.g. a camera another app holds: after 2 s, then twice as long
 * each time up to 30 s (a node that is gone stays gone until plinius-usb names a new one; on
 * 13.11.2026 the helper tried every 2 s for 18 minutes) */
#define RETRY_MIN_S 2
#define RETRY_MAX_S 30

static struct {
	GMainLoop *loop;
	GDBusProxy *usb;
	/* Out pipeline and the node it streams into */
	GstElement *out;
	char *device;
	/* Camera pipeline and the camera it shows */
	GstElement *camera;
	char *camera_name;
	gboolean streaming;
	guint retry_source;
	guint retry_s;
	/* The node's device number, also once it is gone from /dev */
	dev_t rdev;
} self;

static void update(void);

/* Names come from plinius-usb, which checks them too; they go into a pipeline description */
static gboolean safe_name(const char *text)
{
	return text && *text && strlen(text) <= 64 &&
	       strspn(text, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./") == strlen(text);
}

static char *usb_string(const char *property)
{
	g_autoptr(GVariant) value = self.usb ? g_dbus_proxy_get_cached_property(self.usb, property) : NULL;

	if (!value || !g_variant_is_of_type(value, G_VARIANT_TYPE_STRING))
		return g_strdup("");
	return g_variant_dup_string(value, NULL);
}

static void stop_pipeline(GstElement **pipeline)
{
	if (!*pipeline)
		return;
	gst_element_set_state(*pipeline, GST_STATE_NULL);
	gst_clear_object(pipeline);
}

/* A stopped out pipeline leaves the node open: GStreamer's uvcsink (1.28.9, also main in
 * November 2026) dup3()s the device fd for its event poll and never closes it, and when the
 * cable was pulled its READY_TO_NULL fails on VIDIOC_UNSUBSCRIBE_EVENT before v4l2sink closes
 * its own. The kernel waits for the last close when the gadget goes (uvc_function_unbind), so
 * plinius-usb had to kill the helper each time. Closing fds that GStreamer owns is not safe (it
 * may close the number again later): the helper ends and systemd starts it again
 * (Restart=on-failure), which closes them all. */
static void end_if_node_held(void)
{
	g_autoptr(GDir) dir = NULL;
	const char *name;

	if (!self.rdev)
		return;
	dir = g_dir_open("/proc/self/fd", 0, NULL);
	while (dir && (name = g_dir_read_name(dir))) {
		struct stat st;

		if (fstat(atoi(name), &st) == 0 && S_ISCHR(st.st_mode) && st.st_rdev == self.rdev) {
			g_message("uvcsink keeps the webcam node open: ending, so that it closes");
			stop_pipeline(&self.camera);
			exit(75);
		}
	}
	self.rdev = 0;
}

static gboolean on_retry(gpointer user_data)
{
	self.retry_source = 0;
	update();
	return G_SOURCE_REMOVE;
}

static void retry_later(void)
{
	if (self.retry_source)
		return;
	self.retry_s = self.retry_s ? MIN(self.retry_s * 2, RETRY_MAX_S) : RETRY_MIN_S;
	self.retry_source = g_timeout_add_seconds(self.retry_s, on_retry, NULL);
}

static gboolean on_bus_message(GstBus *bus, GstMessage *message, gpointer user_data)
{
	GstElement **pipeline = user_data;
	g_autoptr(GError) error = NULL;
	g_autofree char *debug = NULL;

	if (GST_MESSAGE_TYPE(message) != GST_MESSAGE_ERROR && GST_MESSAGE_TYPE(message) != GST_MESSAGE_EOS)
		return G_SOURCE_CONTINUE;
	if (GST_MESSAGE_TYPE(message) == GST_MESSAGE_ERROR) {
		gst_message_parse_error(message, &error, &debug);
		g_warning("%s pipeline: %s (%s)", pipeline == &self.out ? "Out" : "Camera", error->message,
			  debug ?: "");
	} else {
		g_warning("%s pipeline ended", pipeline == &self.out ? "Out" : "Camera");
	}
	if (pipeline == &self.out) {
		/* Without the out pipeline the camera has nowhere to go */
		stop_pipeline(&self.camera);
		g_clear_pointer(&self.device, g_free);
		self.streaming = FALSE;
	}
	stop_pipeline(pipeline);
	if (pipeline == &self.out)
		end_if_node_held();
	retry_later();
	return G_SOURCE_REMOVE;
}

static GstElement *start_pipeline(const char *description, GstElement **slot)
{
	g_autoptr(GError) error = NULL;
	g_autoptr(GstBus) bus = NULL;
	GstElement *pipeline = gst_parse_launch(description, &error);

	if (!pipeline) {
		g_warning("Cannot build %s: %s", description, error->message);
		return NULL;
	}
	bus = gst_element_get_bus(pipeline);
	gst_bus_add_watch(bus, on_bus_message, slot);
	if (gst_element_set_state(pipeline, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
		g_warning("Cannot start %s", description);
		gst_element_set_state(pipeline, GST_STATE_NULL);
		gst_object_unref(pipeline);
		return NULL;
	}
	return pipeline;
}

/* uvcsink tells it from its own thread */
static gboolean on_streaming_changed_idle(gpointer user_data)
{
	gboolean streaming = FALSE;
	GstElement *sink = self.out ? gst_bin_get_by_name(GST_BIN(self.out), "sink") : NULL;

	if (sink) {
		g_object_get(sink, "streaming", &streaming, NULL);
		gst_object_unref(sink);
	}
	if (streaming != self.streaming) {
		self.streaming = streaming;
		g_message("The computer %s the picture", streaming ? "takes" : "no longer takes");
		update();
	}
	return G_SOURCE_REMOVE;
}

static void on_streaming_notify(GObject *sink, GParamSpec *pspec, gpointer user_data)
{
	g_idle_add(on_streaming_changed_idle, NULL);
}

static void start_out(const char *device)
{
	g_autofree char *description = NULL;
	GstElement *sink;

	/* uvcsink starts the stream only once the caps it gets are part of what the computer chose:
	 * image/jpeg with parsed=true and the colorimetry of the UVC descriptors (BT.709, sRGB
	 * transfer: 2:4:7:1). jpegenc sets neither; jpegparse would set bt601 and let the size go. */
	description = g_strdup_printf(
		"intervideosrc channel=" CHANNEL " ! videoconvert ! videoscale ! "
		"video/x-raw,pixel-aspect-ratio=1/1 ! jpegenc quality=85 ! "
		"capssetter caps=\"image/jpeg,parsed=(boolean)true,colorimetry=(string)2:4:7:1\" ! "
		"uvcsink name=sink v4l2sink::device=%s",
		device);
	{
		struct stat st;

		self.rdev = stat(device, &st) == 0 && S_ISCHR(st.st_mode) ? st.st_rdev : 0;
	}
	self.out = start_pipeline(description, &self.out);
	if (!self.out) {
		/* A start that failed after uvcsink opened its event fd */
		end_if_node_held();
		retry_later();
		return;
	}
	self.device = g_strdup(device);
	self.retry_s = 0;
	sink = gst_bin_get_by_name(GST_BIN(self.out), "sink");
	g_signal_connect(sink, "notify::streaming", G_CALLBACK(on_streaming_notify), NULL);
	gst_object_unref(sink);
	g_message("Webcam on %s", device);
}

static void start_camera(const char *camera)
{
	g_autofree char *description = g_strdup_printf(
		"pipewiresrc target-object=%s ! videoflip video-direction=auto ! intervideosink channel=" CHANNEL,
		camera);

	self.camera = start_pipeline(description, &self.camera);
	if (!self.camera) {
		retry_later();
		return;
	}
	self.camera_name = g_strdup(camera);
	g_message("Camera %s on", camera);
}

static void stop_camera(void)
{
	if (!self.camera)
		return;
	stop_pipeline(&self.camera);
	g_message("Camera %s off", self.camera_name);
	g_clear_pointer(&self.camera_name, g_free);
}

/* Brings both pipelines to what plinius-usb and the computer want now */
static void update(void)
{
	g_autofree char *device = usb_string("WebcamDevice");
	g_autofree char *camera = usb_string("WebcamCamera");
	gboolean want_camera;

	if (*device && !safe_name(device)) {
		g_free(device);
		device = g_strdup("");
	}
	if (!safe_name(camera))
		g_clear_pointer(&camera, g_free);

	if (g_strcmp0(device, self.device)) {
		stop_camera();
		stop_pipeline(&self.out);
		end_if_node_held();
		g_clear_pointer(&self.device, g_free);
		self.streaming = FALSE;
		if (!*device) {
			g_clear_handle_id(&self.retry_source, g_source_remove);
			self.retry_s = 0;
			g_message("Webcam off");
		} else if (!g_file_test(device, G_FILE_TEST_EXISTS)) {
			/* Not there (yet): no pipeline that can only fail */
			retry_later();
		} else {
			start_out(device);
		}
	}

	want_camera = self.out && self.streaming && camera;
	if (self.camera && (!want_camera || g_strcmp0(camera, self.camera_name)))
		stop_camera();
	if (want_camera && !self.camera)
		start_camera(camera);
}

static void on_properties_changed(GDBusProxy *proxy, GVariant *changed, GStrv invalidated, gpointer user_data)
{
	update();
}

static void on_name_owner(GObject *proxy, GParamSpec *pspec, gpointer user_data)
{
	update();
}

static gboolean on_signal(gpointer user_data)
{
	g_main_loop_quit(self.loop);
	return G_SOURCE_REMOVE;
}

int main(int argc, char **argv)
{
	g_autoptr(GError) error = NULL;

	gst_init(&argc, &argv);
	self.usb = g_dbus_proxy_new_for_bus_sync(G_BUS_TYPE_SYSTEM, G_DBUS_PROXY_FLAGS_NONE, NULL, USB_NAME,
						 USB_PATH, USB_INTERFACE, NULL, &error);
	if (!self.usb) {
		g_printerr("Cannot reach %s: %s\n", USB_NAME, error->message);
		return 1;
	}
	g_signal_connect(self.usb, "g-properties-changed", G_CALLBACK(on_properties_changed), NULL);
	/* plinius-usb restarted: its properties come back with it */
	g_signal_connect(self.usb, "notify::g-name-owner", G_CALLBACK(on_name_owner), NULL);

	self.loop = g_main_loop_new(NULL, FALSE);
	g_unix_signal_add(SIGTERM, on_signal, NULL);
	g_unix_signal_add(SIGINT, on_signal, NULL);
	update();
	g_main_loop_run(self.loop);

	stop_camera();
	stop_pipeline(&self.out);
	return 0;
}
