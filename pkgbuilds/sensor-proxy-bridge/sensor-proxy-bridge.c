// SPDX-License-Identifier: MIT
// sensor-proxy-bridge: the Android sensors HAL (AIDL android.hardware.sensors.ISensors over
// binder) behind the D-Bus API of iio-sensor-proxy (net.hadess.SensorProxy), which Phosh and
// GNOME use: screen rotation, proximity (display off in calls), ambient light and the compass
// (geoclue). The compass heading comes from the geomagnetic rotation vector (magnetometer and
// accelerometer, fused on the sensor hub): measured on the Plinius, it is as right as the
// orientation sensor but follows a turn at once, where that one drifts after it for a minute.
//
// The HAL delivers events through a fast message queue (shared memory) that the client creates
// and hands over in initialize(). A reader thread waits on it and passes the values to the main
// loop. A sensor is only active while at least one D-Bus client has claimed it.
//
// Plinius addition on the same object: org.plinius.SensorProxyBridge.OrientationSettleTime, how
// long a new orientation must hold before the screen turns (GNOME Settings > Display); and the
// sensor hub's pick-up gesture (ClaimPickUp, signal PickedUp) for plinius-wake. That is a wakeup
// sensor: it also fires while the phone is suspended, without keeping the accelerometer running.
// On its own object /net/hadess/SensorProxy/Gravity, open to every user: the sensor hub's gravity
// vector at 50 Hz (ClaimGravity, signal Gravity), for plinius-ambient's rolling balls; and the
// linear acceleration (acceleration without gravity, ClaimMotion, signal Motion) at 50 Hz, which
// lets plinius-ambient's water slosh when the phone is moved; and the rotation vector (gyroscope,
// accelerometer and magnetometer fused on the sensor hub, ClaimOrientation, signal Orientation)
// as a unit quaternion at 50 Hz, by which 360° photos turn with the phone (the background of
// plinius-ambient, Muga's viewer). Without that sensor the game rotation vector stands in (no
// magnetometer, so no north); OrientationReference says which one it is. And the game rotation
// vector itself (ClaimGameOrientation, signal GameOrientation, the same quaternion), for views
// that need no north: measured on the Plinius (0.8), the rotation vector's view lagged behind a
// turn and kept moving on the table, about 4° a minute, as the sensor hub pulled the heading
// towards the magnetometer's. plinius-ambient's 360° background takes this one.
#include <gbinder.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <math.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>

#define HAL_DEVICE "/dev/binder"
#define HAL_IFACE "android.hardware.sensors.ISensors"
#define HAL_CALLBACK_IFACE "android.hardware.sensors.ISensorsCallback"
/* How long to wait for the HAL to show up at start */
#define HAL_WAIT_S 60

/* ISensors transactions, in the order of ISensors.aidl */
enum { HAL_ACTIVATE = 1, HAL_BATCH = 2, HAL_GET_SENSORS_LIST = 5, HAL_INITIALIZE = 6 };
/* Event flag bits of the queues (ISensors.aidl) */
enum { EVENT_QUEUE_READ_AND_PROCESS = 1, EVENT_QUEUE_EVENTS_READ = 2, WAKE_LOCK_QUEUE_DATA_WRITTEN = 1 };
/* SensorType values and SensorInfo flags */
enum { TYPE_ACCELEROMETER = 1, TYPE_LIGHT = 5, TYPE_PROXIMITY = 8, TYPE_GRAVITY = 9,
       TYPE_LINEAR_ACCELERATION = 10, TYPE_ROTATION_VECTOR = 11, TYPE_GAME_ROTATION_VECTOR = 15, TYPE_GEOMAGNETIC_ROTATION_VECTOR = 20,
       TYPE_PICK_UP_GESTURE = 25 };
#define SENSOR_FLAG_WAKE_UP 1
/* Reporting mode in bits 1-3: one-shot sensors switch themselves off after an event */
#define SENSOR_FLAG_REPORTING_MODE 0xe
#define SENSOR_FLAG_ONE_SHOT 0x4

/* An event as the HAL writes it into the queue: the memory layout of the @FixedSize parcelable
 * android.hardware.sensors.Event (96 bytes) */
struct hal_event {
	int64_t timestamp;
	int32_t sensor;
	int32_t type;
	int32_t payload_tag;
	int32_t padding;
	union {
		struct {
			float x, y, z;
		} vec3;
		float data[16]; /* rotation vectors: x, y, z, w (w may be 0: then from the others) */
		float scalar;
		uint8_t raw[72];
	} payload;
};
G_STATIC_ASSERT(sizeof(struct hal_event) == 96);

#define QUEUE_EVENTS 128
#define STANDARD_GRAVITY 9.80665f

enum sensor_id { ACCELEROMETER, LIGHT, PROXIMITY, COMPASS, PICK_UP, GRAVITY, MOTION, ORIENTATION, GAME_ORIENTATION,
		N_SENSORS };

struct sensor {
	const char *name;    /* in the D-Bus method names: Claim<name> */
	int32_t type;
	int32_t handle;      /* -1: not available */
	gboolean wakeup;
	float max_range;
	int64_t period_ns;
	GHashTable *claims; /* D-Bus unique name -> watch id */
	gboolean one_shot;
};

static struct {
	GMainLoop *loop;
	GDBusConnection *bus;
	guint object_id;
	GBinderServiceManager *sm;
	GBinderRemoteObject *remote;
	GBinderClient *client;
	GBinderLocalObject *callback;
	GBinderFmq *events;
	GBinderFmq *wake_lock;
	GThread *reader;
	volatile gint quit;
	struct sensor sensors[N_SENSORS];
	/* D-Bus state, main loop only */
	const char *orientation;
	const char *tilt;
	const char *pending_orientation;
	gint64 pending_since;
	/* How long a new orientation must hold, in ms (org.plinius.SensorProxyBridge) */
	guint32 settle_ms;
	double light_level;
	gboolean near;
	double heading;
	/* The orientation is the rotation vector (north) or the game rotation vector (relative) */
	gboolean orientation_north;
} self = {
	.sensors = {
		[ACCELEROMETER] = {"Accelerometer", TYPE_ACCELEROMETER, -1, FALSE, 0, 100000000, NULL},
		[LIGHT] = {"Light", TYPE_LIGHT, -1, FALSE, 0, 500000000, NULL},
		[PROXIMITY] = {"Proximity", TYPE_PROXIMITY, -1, FALSE, 0, 100000000, NULL},
		[COMPASS] = {"Compass", TYPE_GEOMAGNETIC_ROTATION_VECTOR, -1, FALSE, 0, 200000000, NULL},
		[PICK_UP] = {"PickUp", TYPE_PICK_UP_GESTURE, -1, FALSE, 0, 0, NULL},
		/* 50 Hz: smooth enough for animations that follow the tilt */
		[GRAVITY] = {"Gravity", TYPE_GRAVITY, -1, FALSE, 0, 20000000, NULL},
		/* 50 Hz as well: a shake swings at 2-6 Hz */
		[MOTION] = {"Motion", TYPE_LINEAR_ACCELERATION, -1, FALSE, 0, 20000000, NULL},
		/* 50 Hz: the clients smooth between samples. With the magnetometer the heading is
		 * true to the compass and does not drift (but a magnet nearby turns it). */
		[ORIENTATION] = {"Orientation", TYPE_ROTATION_VECTOR, -1, FALSE, 0, 20000000, NULL},
		/* 50 Hz: the gyroscope alone, followed at once and still on a table, but no north */
		[GAME_ORIENTATION] = {"GameOrientation", TYPE_GAME_ROTATION_VECTOR, -1, FALSE, 0, 20000000, NULL},
	},
	.heading = -1,
	.orientation = "undefined",
	.settle_ms = 500,
	.tilt = "vertical",
};

static const char introspection[] =
	"<node>"
	" <interface name='net.hadess.SensorProxy'>"
	"  <method name='ClaimAccelerometer'/>"
	"  <method name='ReleaseAccelerometer'/>"
	"  <method name='ClaimLight'/>"
	"  <method name='ReleaseLight'/>"
	"  <method name='ClaimProximity'/>"
	"  <method name='ReleaseProximity'/>"
	"  <property name='HasAccelerometer' type='b' access='read'/>"
	"  <property name='AccelerometerOrientation' type='s' access='read'/>"
	"  <property name='AccelerometerTilt' type='s' access='read'/>"
	"  <property name='HasAmbientLight' type='b' access='read'/>"
	"  <property name='LightLevelUnit' type='s' access='read'/>"
	"  <property name='LightLevel' type='d' access='read'/>"
	"  <property name='HasProximity' type='b' access='read'/>"
	"  <property name='ProximityNear' type='b' access='read'/>"
	" </interface>"
	" <interface name='org.plinius.SensorProxyBridge'>"
	"  <method name='ClaimPickUp'/>"
	"  <method name='ReleasePickUp'/>"
	"  <property name='OrientationSettleTime' type='u' access='readwrite'/>"
	"  <property name='HasPickUp' type='b' access='read'/>"
	"  <signal name='PickedUp'/>"
	" </interface>"
	" <interface name='net.hadess.SensorProxy.Compass'>"
	"  <method name='ClaimCompass'/>"
	"  <method name='ReleaseCompass'/>"
	"  <property name='HasCompass' type='b' access='read'/>"
	"  <property name='CompassHeading' type='d' access='read'/>"
	" </interface>"
	" <interface name='org.plinius.SensorProxyBridge.Gravity'>"
	"  <method name='ClaimGravity'/>"
	"  <method name='ReleaseGravity'/>"
	"  <property name='HasGravity' type='b' access='read'/>"
	"  <signal name='Gravity'><arg name='x' type='d'/><arg name='y' type='d'/><arg name='z' type='d'/></signal>"
	"  <method name='ClaimMotion'/>"
	"  <method name='ReleaseMotion'/>"
	"  <property name='HasMotion' type='b' access='read'/>"
	"  <signal name='Motion'><arg name='x' type='d'/><arg name='y' type='d'/><arg name='z' type='d'/></signal>"
	"  <method name='ClaimOrientation'/>"
	"  <method name='ReleaseOrientation'/>"
	"  <property name='HasOrientation' type='b' access='read'/>"
	"  <property name='OrientationReference' type='s' access='read'/>"
	"  <signal name='Orientation'><arg name='x' type='d'/><arg name='y' type='d'/><arg name='z' type='d'/>"
	"<arg name='w' type='d'/></signal>"
	"  <method name='ClaimGameOrientation'/>"
	"  <method name='ReleaseGameOrientation'/>"
	"  <property name='HasGameOrientation' type='b' access='read'/>"
	"  <signal name='GameOrientation'><arg name='x' type='d'/><arg name='y' type='d'/><arg name='z' type='d'/>"
	"<arg name='w' type='d'/></signal>"
	" </interface>"
	"</node>";

/* ---- HAL ---- */

/* Sends a request and checks the AIDL status (exception code) in the reply */
static gboolean hal_call(guint32 code, GBinderLocalRequest *req)
{
	int status;
	GBinderRemoteReply *reply = gbinder_client_transact_sync_reply(self.client, code, req, &status);
	gint32 exception = -1;
	if (reply) {
		GBinderReader reader;
		gbinder_remote_reply_init_reader(reply, &reader);
		gbinder_reader_read_int32(&reader, &exception);
		gbinder_remote_reply_unref(reply);
	}
	if (status != GBINDER_STATUS_OK || exception != 0) {
		g_warning("ISensors call %u failed (status %d, exception %d)", code, status, exception);
		return FALSE;
	}
	return TRUE;
}

static void hal_set_active(struct sensor *s, gboolean active)
{
	GBinderLocalRequest *req;
	if (active) {
		req = gbinder_client_new_request(self.client);
		gbinder_local_request_append_int32(req, s->handle);
		gbinder_local_request_append_int64(req, s->period_ns);
		gbinder_local_request_append_int64(req, 0);
		hal_call(HAL_BATCH, req);
		gbinder_local_request_unref(req);
	}
	req = gbinder_client_new_request(self.client);
	gbinder_local_request_append_int32(req, s->handle);
	gbinder_local_request_append_int32(req, active);
	hal_call(HAL_ACTIVATE, req);
	gbinder_local_request_unref(req);
	g_info("%s %s", s->name, active ? "on" : "off");
}

/* Picks the first sensor of each type we serve; a non-wakeup accelerometer is preferred */
static void hal_read_sensor_list(void)
{
	int status;
	GBinderRemoteReply *reply = gbinder_client_transact_sync_reply(self.client, HAL_GET_SENSORS_LIST, NULL,
								       &status);
	if (!reply)
		return;
	GBinderReader r;
	gint32 exception = -1, count = 0, game_rotation = -1;
	gboolean game_rotation_wakeup = FALSE;
	gbinder_remote_reply_init_reader(reply, &r);
	gbinder_reader_read_int32(&r, &exception);
	gbinder_reader_read_int32(&r, &count);
	for (gint32 i = 0; exception == 0 && i < count; i++) {
		gint32 present, size, handle, version, type, min_delay, max_delay;
		guint32 fifo_reserved, fifo_max, flags;
		gfloat max_range, resolution, power;
		/* Non-null marker and size of the SensorInfo parcelable */
		gbinder_reader_read_int32(&r, &present);
		gbinder_reader_read_int32(&r, &size);
		gbinder_reader_read_int32(&r, &handle);
		char *name = gbinder_reader_read_string16(&r);
		gbinder_reader_skip_string16(&r); /* vendor */
		gbinder_reader_read_int32(&r, &version);
		gbinder_reader_read_int32(&r, &type);
		gbinder_reader_skip_string16(&r); /* typeAsString */
		gbinder_reader_read_float(&r, &max_range);
		gbinder_reader_read_float(&r, &resolution);
		gbinder_reader_read_float(&r, &power);
		gbinder_reader_read_int32(&r, &min_delay);
		gbinder_reader_read_uint32(&r, &fifo_reserved);
		gbinder_reader_read_uint32(&r, &fifo_max);
		gbinder_reader_skip_string16(&r); /* requiredPermission */
		gbinder_reader_read_int32(&r, &max_delay);
		gbinder_reader_read_uint32(&r, &flags);
		if (type == TYPE_GAME_ROTATION_VECTOR && game_rotation < 0) {
			game_rotation = handle;
			game_rotation_wakeup = (flags & SENSOR_FLAG_WAKE_UP) != 0;
		}
		for (int id = 0; id < N_SENSORS; id++) {
			struct sensor *s = &self.sensors[id];
			gboolean wakeup = (flags & SENSOR_FLAG_WAKE_UP) != 0;
			if (type != s->type)
				continue;
			if (s->handle >= 0 && !(id == ACCELEROMETER && s->wakeup && !wakeup))
				continue;
			s->handle = handle;
			s->wakeup = wakeup;
			s->max_range = max_range;
			s->one_shot = (flags & SENSOR_FLAG_REPORTING_MODE) == SENSOR_FLAG_ONE_SHOT;
			g_info("%s: handle %d \"%s\"%s, max range %.1f", s->name, handle, name,
			       wakeup ? " (wakeup)" : "", max_range);
		}
		g_free(name);
	}
	gbinder_remote_reply_unref(reply);
	struct sensor *o = &self.sensors[ORIENTATION];
	self.orientation_north = o->handle >= 0;
	if (o->handle < 0 && game_rotation >= 0) {
		o->handle = game_rotation;
		o->wakeup = game_rotation_wakeup;
		g_info("Orientation: no rotation vector, the game rotation vector (handle %d) without north",
		       game_rotation);
	}
}

static GBinderLocalReply *on_hal_callback(GBinderLocalObject *obj, GBinderRemoteRequest *req, guint code,
					  guint flags, int *status, void *user_data)
{
	/* Dynamic sensors (dis)connected: not used */
	*status = GBINDER_STATUS_OK;
	return NULL;
}

static void on_hal_died(GBinderRemoteObject *obj, void *user_data)
{
	g_warning("Sensors HAL died");
	g_main_loop_quit(self.loop);
}

static gboolean hal_connect(void)
{
	int status;
	self.sm = gbinder_servicemanager_new(HAL_DEVICE);
	if (!self.sm || !gbinder_servicemanager_wait(self.sm, -1))
		return FALSE;
	/* The HAL registers some seconds after the Android container has started */
	for (int tries = 0; !self.remote && tries < HAL_WAIT_S * 2; tries++) {
		self.remote = gbinder_servicemanager_get_service_sync(self.sm, HAL_IFACE "/default", &status);
		if (!self.remote)
			g_usleep(G_USEC_PER_SEC / 2);
	}
	if (!self.remote)
		return FALSE;
	gbinder_remote_object_ref(self.remote);
	gbinder_remote_object_add_death_handler(self.remote, on_hal_died, NULL);
	self.client = gbinder_client_new(self.remote, HAL_IFACE);

	self.callback = gbinder_servicemanager_new_local_object(self.sm, HAL_CALLBACK_IFACE, on_hal_callback, NULL);
	gbinder_local_object_set_stability(self.callback, GBINDER_STABILITY_VINTF);
	self.events = gbinder_fmq_new(sizeof(struct hal_event), QUEUE_EVENTS, GBINDER_FMQ_TYPE_SYNC_READ_WRITE,
				      GBINDER_FMQ_FLAG_CONFIGURE_EVENT_FLAG, -1, 0);
	self.wake_lock = gbinder_fmq_new(sizeof(guint32), QUEUE_EVENTS, GBINDER_FMQ_TYPE_SYNC_READ_WRITE,
					 GBINDER_FMQ_FLAG_CONFIGURE_EVENT_FLAG, -1, 0);
	GBinderLocalRequest *req = gbinder_client_new_request(self.client);
	GBinderWriter writer;
	gbinder_local_request_init_writer(req, &writer);
	gbinder_writer_append_fmq_descriptor(&writer, self.events);
	gbinder_writer_append_fmq_descriptor(&writer, self.wake_lock);
	gbinder_writer_append_local_object(&writer, self.callback);
	gboolean ok = hal_call(HAL_INITIALIZE, req);
	gbinder_local_request_unref(req);
	if (ok)
		hal_read_sensor_list();
	return ok;
}

/* ---- Values to D-Bus properties (main loop) ---- */

static void emit_changed_on(const char *path, const char *iface, const char *property, GVariant *value)
{
	GVariantBuilder changed;
	g_variant_builder_init(&changed, G_VARIANT_TYPE("a{sv}"));
	g_variant_builder_add(&changed, "{sv}", property, value);
	g_dbus_connection_emit_signal(self.bus, NULL, path, "org.freedesktop.DBus.Properties", "PropertiesChanged",
				      g_variant_new("(sa{sv}as)", iface, &changed, NULL), NULL);
}

static void emit_changed(const char *property, GVariant *value)
{
	emit_changed_on("/net/hadess/SensorProxy", "net.hadess.SensorProxy", property, value);
}

/* Orientations as iio-sensor-proxy names them, clockwise from upright in 90 degree steps */
static const char *const orientations[] = {"normal", "right-up", "bottom-up", "left-up"};
/* The screen plane must be tilted at least this far from flat */
#define ORIENTATION_MIN_TILT 35.0
/* Degrees beyond the 45 degree halfway mark before the current orientation is left */
#define ORIENTATION_HYSTERESIS 15.0
/* Range of the settle time (how long a new orientation must hold before it is reported);
 * set in GNOME Settings > Display, kept in the state directory */
#define SETTLE_MS_MIN 100
#define SETTLE_MS_MAX 3000
#define SETTINGS_FILE "/var/lib/sensor-proxy-bridge/settings"

/* Orientation from the gravity vector in Android's axes (x right, y up, z out of the screen):
 * the quarter its direction in the screen plane points to. The current orientation is kept
 * until the phone is turned 45 + ORIENTATION_HYSTERESIS degrees away from it, so a slight turn
 * does not rotate the screen. Lying (nearly) flat keeps the last orientation. */
static const char *orientation_of(float x, float y, float z, const char *current)
{
	if (atan2(hypot(x, y), fabs(z)) * 180.0 / M_PI < ORIENTATION_MIN_TILT)
		return NULL;
	double angle = atan2(x, y) * 180.0 / M_PI; /* clockwise from upright */
	if (angle < 0)
		angle += 360.0;
	const char *nearest = orientations[(int)lround(angle / 90.0) % 4];
	for (int i = 0; i < 4; i++) {
		if (strcmp(current, orientations[i]))
			continue;
		double away = fabs(angle - i * 90.0);
		if (away > 180.0)
			away = 360.0 - away;
		return away > 45.0 + ORIENTATION_HYSTERESIS ? nearest : current;
	}
	return nearest; /* no orientation yet */
}

static const char *tilt_of(float z)
{
	float g = z / STANDARD_GRAVITY;
	if (g > 0.9f)
		return "face-up";
	if (g < -0.9f)
		return "face-down";
	if (g > 0.35f)
		return "tilted-up";
	if (g < -0.35f)
		return "tilted-down";
	return "vertical";
}

static void on_accelerometer(float x, float y, float z)
{
	const char *orientation = orientation_of(x, y, z, self.orientation);
	/* A new orientation must hold for the settle time: no rotation while the phone is only
	 * being moved */
	gint64 now = g_get_monotonic_time();
	if (orientation && strcmp(orientation, self.orientation)) {
		if (!self.pending_orientation || strcmp(orientation, self.pending_orientation)) {
			self.pending_orientation = orientation;
			self.pending_since = now;
		} else if (now - self.pending_since >= (gint64)self.settle_ms * G_TIME_SPAN_MILLISECOND) {
			self.orientation = orientation;
			self.pending_orientation = NULL;
			emit_changed("AccelerometerOrientation", g_variant_new_string(orientation));
		}
	} else {
		self.pending_orientation = NULL;
	}
	const char *tilt = tilt_of(z);
	if (strcmp(tilt, self.tilt)) {
		self.tilt = tilt;
		emit_changed("AccelerometerTilt", g_variant_new_string(tilt));
	}
}

static void on_light(float lux)
{
	if (fabs(lux - self.light_level) < 0.5)
		return;
	self.light_level = lux;
	emit_changed("LightLevel", g_variant_new_double(lux));
}

static void on_proximity(float distance)
{
	/* Binary sensors report 0 (near) or their maximum range (far) */
	gboolean near = distance < self.sensors[PROXIMITY].max_range;
	if (near == self.near)
		return;
	self.near = near;
	emit_changed("ProximityNear", g_variant_new_boolean(near));
}

/* Degrees from magnetic north, clockwise; small changes are not reported */
#define HEADING_MIN_CHANGE 1.0

/* Azimuth of a rotation vector (unit quaternion) as Android's SensorManager computes it:
 * getRotationMatrixFromVector, then getOrientation's atan2(R[1], R[4]) */
static double azimuth_of(const float *q)
{
	double x = q[0], y = q[1], z = q[2], w = q[3];
	if (w == 0) {
		w = 1 - x * x - y * y - z * z;
		w = w > 0 ? sqrt(w) : 0;
	}
	return atan2(2 * (x * y - z * w), 1 - 2 * (x * x + z * z)) * 180.0 / M_PI;
}

static void on_compass(const float *rotation)
{
	double heading = fmod(azimuth_of(rotation), 360.0);
	if (heading < 0)
		heading += 360.0;
	double change = fabs(heading - self.heading);
	if (self.heading >= 0 && MIN(change, 360.0 - change) < HEADING_MIN_CHANGE)
		return;
	self.heading = heading;
	emit_changed_on("/net/hadess/SensorProxy/Compass", "net.hadess.SensorProxy.Compass", "CompassHeading",
			g_variant_new_double(heading));
}

static void on_pick_up(void)
{
	struct sensor *s = &self.sensors[PICK_UP];
	g_dbus_connection_emit_signal(self.bus, NULL, "/net/hadess/SensorProxy", "org.plinius.SensorProxyBridge",
				      "PickedUp", NULL, NULL);
	/* A one-shot sensor is off now: arm it again while it is claimed */
	if (s->one_shot && g_hash_table_size(s->claims))
		hal_set_active(s, TRUE);
}

/* Every sample goes out as a signal (m/s², sensor axes: x right, y up, z out of the screen; at
 * rest the vector points up, against gravity) */
static void on_gravity(float x, float y, float z)
{
	g_dbus_connection_emit_signal(self.bus, NULL, "/net/hadess/SensorProxy/Gravity",
				      "org.plinius.SensorProxyBridge.Gravity", "Gravity",
				      g_variant_new("(ddd)", (double)x, (double)y, (double)z), NULL);
}

/* Every sample goes out as a signal (m/s², same axes as Gravity; zero at rest) */
static void on_motion(float x, float y, float z)
{
	g_dbus_connection_emit_signal(self.bus, NULL, "/net/hadess/SensorProxy/Gravity",
				      "org.plinius.SensorProxyBridge.Gravity", "Motion",
				      g_variant_new("(ddd)", (double)x, (double)y, (double)z), NULL);
}

/* Every sample goes out as a signal: the rotation from the device's axes (as for Gravity) into
 * a world with z up and y to magnetic north (to an arbitrary heading for the game rotation
 * vector), as a unit quaternion x, y, z, w (Android's order) */
static void on_orientation(const char *signal, const float *q)
{
	double x = q[0], y = q[1], z = q[2], w = q[3];
	if (w == 0) {
		w = 1 - x * x - y * y - z * z;
		w = w > 0 ? sqrt(w) : 0;
	}
	g_dbus_connection_emit_signal(self.bus, NULL, "/net/hadess/SensorProxy/Gravity",
				      "org.plinius.SensorProxyBridge.Gravity", signal,
				      g_variant_new("(dddd)", x, y, z, w), NULL);
}

static gboolean dispatch_events(gpointer data)
{
	GArray *batch = data;
	for (guint i = 0; i < batch->len; i++) {
		const struct hal_event *e = &g_array_index(batch, struct hal_event, i);
		if (e->sensor == self.sensors[ACCELEROMETER].handle)
			on_accelerometer(e->payload.vec3.x, e->payload.vec3.y, e->payload.vec3.z);
		else if (e->sensor == self.sensors[LIGHT].handle)
			on_light(e->payload.scalar);
		else if (e->sensor == self.sensors[PROXIMITY].handle)
			on_proximity(e->payload.scalar);
		else if (e->sensor == self.sensors[COMPASS].handle)
			on_compass(e->payload.data);
		else if (e->sensor == self.sensors[PICK_UP].handle)
			on_pick_up();
		else if (e->sensor == self.sensors[GRAVITY].handle)
			on_gravity(e->payload.vec3.x, e->payload.vec3.y, e->payload.vec3.z);
		else if (e->sensor == self.sensors[MOTION].handle)
			on_motion(e->payload.vec3.x, e->payload.vec3.y, e->payload.vec3.z);
		else {
			/* Both may be the game rotation vector (no rotation vector): each its signal */
			if (e->sensor == self.sensors[ORIENTATION].handle)
				on_orientation("Orientation", e->payload.data);
			if (e->sensor == self.sensors[GAME_ORIENTATION].handle)
				on_orientation("GameOrientation", e->payload.data);
		}
	}
	g_array_unref(batch);
	return G_SOURCE_REMOVE;
}

/* ---- Event queue (reader thread) ---- */

static gpointer reader_thread(gpointer data)
{
	struct hal_event buffer[QUEUE_EVENTS];
	while (!g_atomic_int_get(&self.quit)) {
		gsize available = gbinder_fmq_available_to_read(self.events);
		if (!available) {
			guint32 state = 0;
			/* With a timeout, so that the thread notices when to quit */
			if (gbinder_fmq_wait_timeout(self.events, EVENT_QUEUE_READ_AND_PROCESS, &state, 1000) < 0)
				continue;
			available = gbinder_fmq_available_to_read(self.events);
		}
		gsize n = MIN(available, G_N_ELEMENTS(buffer));
		if (!n || !gbinder_fmq_read(self.events, buffer, n))
			continue;
		gbinder_fmq_wake(self.events, EVENT_QUEUE_EVENTS_READ);

		/* Per event, not per entry: sensors may share one HAL sensor (and its handle) */
		guint32 wakeups = 0;
		for (gsize i = 0; i < n; i++)
			for (int id = 0; id < N_SENSORS; id++)
				if (buffer[i].sensor == self.sensors[id].handle && self.sensors[id].wakeup) {
					wakeups++;
					break;
				}
		/* Wakeup events hold a wake lock in the HAL until we report them as handled */
		if (wakeups && gbinder_fmq_write(self.wake_lock, &wakeups, 1))
			gbinder_fmq_wake(self.wake_lock, WAKE_LOCK_QUEUE_DATA_WRITTEN);

		GArray *batch = g_array_sized_new(FALSE, FALSE, sizeof(struct hal_event), n);
		g_array_append_vals(batch, buffer, n);
		g_idle_add(dispatch_events, batch);
	}
	return NULL;
}

/* ---- D-Bus ---- */

static void unwatch(gpointer watch)
{
	g_bus_unwatch_name(GPOINTER_TO_UINT(watch));
}

/* Another of ours on the same HAL sensor that is claimed (Orientation standing in with the game
 * rotation vector): that one keeps it running */
static gboolean shared_and_claimed(const struct sensor *s)
{
	for (int id = 0; id < N_SENSORS; id++) {
		const struct sensor *other = &self.sensors[id];
		if (other != s && other->handle == s->handle && g_hash_table_size(other->claims))
			return TRUE;
	}
	return FALSE;
}

static void release(struct sensor *s, const char *sender)
{
	if (!g_hash_table_remove(s->claims, sender))
		return;
	if (g_hash_table_size(s->claims) == 0 && !shared_and_claimed(s))
		hal_set_active(s, FALSE);
}

static void on_client_vanished(GDBusConnection *bus, const char *name, gpointer user_data)
{
	for (int id = 0; id < N_SENSORS; id++)
		release(&self.sensors[id], name);
}

static void claim(struct sensor *s, const char *sender)
{
	if (g_hash_table_contains(s->claims, sender))
		return;
	guint watch = g_bus_watch_name_on_connection(self.bus, sender, G_BUS_NAME_WATCHER_FLAGS_NONE, NULL,
						     on_client_vanished, NULL, NULL);
	g_hash_table_insert(s->claims, g_strdup(sender), GUINT_TO_POINTER(watch));
	if (g_hash_table_size(s->claims) > 1)
		return;
	/* The last heading is stale by now: report the first new one in any case */
	if (s == &self.sensors[COMPASS])
		self.heading = -1;
	if (!shared_and_claimed(s))
		hal_set_active(s, TRUE);
}

static void on_method_call(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			   const char *method, GVariant *params, GDBusMethodInvocation *invocation,
			   gpointer user_data)
{
	gboolean is_claim = g_str_has_prefix(method, "Claim");
	const char *name = is_claim ? method + 5 : g_str_has_prefix(method, "Release") ? method + 7 : NULL;
	for (int id = 0; name && id < N_SENSORS; id++) {
		struct sensor *s = &self.sensors[id];
		if (strcmp(name, s->name))
			continue;
		if (s->handle < 0) {
			g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR, G_DBUS_ERROR_FAILED,
							      "No %s", s->name);
			return;
		}
		if (is_claim)
			claim(s, sender);
		else
			release(s, sender);
		g_dbus_method_invocation_return_value(invocation, NULL);
		return;
	}
	g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_METHOD,
					      "Unknown method %s", method);
}

static GVariant *on_get_property(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
				 const char *property, GError **error, gpointer user_data)
{
	if (!strcmp(property, "HasAccelerometer"))
		return g_variant_new_boolean(self.sensors[ACCELEROMETER].handle >= 0);
	if (!strcmp(property, "AccelerometerOrientation"))
		return g_variant_new_string(self.orientation);
	if (!strcmp(property, "AccelerometerTilt"))
		return g_variant_new_string(self.tilt);
	if (!strcmp(property, "HasAmbientLight"))
		return g_variant_new_boolean(self.sensors[LIGHT].handle >= 0);
	if (!strcmp(property, "LightLevelUnit"))
		return g_variant_new_string("lux");
	if (!strcmp(property, "LightLevel"))
		return g_variant_new_double(self.light_level);
	if (!strcmp(property, "HasProximity"))
		return g_variant_new_boolean(self.sensors[PROXIMITY].handle >= 0);
	if (!strcmp(property, "ProximityNear"))
		return g_variant_new_boolean(self.near);
	if (!strcmp(property, "OrientationSettleTime"))
		return g_variant_new_uint32(self.settle_ms);
	if (!strcmp(property, "HasCompass"))
		return g_variant_new_boolean(self.sensors[COMPASS].handle >= 0);
	if (!strcmp(property, "CompassHeading"))
		return g_variant_new_double(self.heading);
	if (!strcmp(property, "HasPickUp"))
		return g_variant_new_boolean(self.sensors[PICK_UP].handle >= 0);
	if (!strcmp(property, "HasGravity"))
		return g_variant_new_boolean(self.sensors[GRAVITY].handle >= 0);
	if (!strcmp(property, "HasMotion"))
		return g_variant_new_boolean(self.sensors[MOTION].handle >= 0);
	if (!strcmp(property, "HasOrientation"))
		return g_variant_new_boolean(self.sensors[ORIENTATION].handle >= 0);
	if (!strcmp(property, "HasGameOrientation"))
		return g_variant_new_boolean(self.sensors[GAME_ORIENTATION].handle >= 0);
	if (!strcmp(property, "OrientationReference"))
		return g_variant_new_string(self.orientation_north ? "north" : "relative");
	g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_PROPERTY, "Unknown property %s", property);
	return NULL;
}

static void load_settings(void)
{
	g_autoptr(GKeyFile) file = g_key_file_new();
	if (!g_key_file_load_from_file(file, SETTINGS_FILE, G_KEY_FILE_NONE, NULL))
		return;
	gint ms = g_key_file_get_integer(file, "orientation", "settle-ms", NULL);
	if (ms >= SETTLE_MS_MIN && ms <= SETTLE_MS_MAX)
		self.settle_ms = ms;
}

static void save_settings(void)
{
	g_autoptr(GKeyFile) file = g_key_file_new();
	g_autoptr(GError) error = NULL;
	g_key_file_set_integer(file, "orientation", "settle-ms", self.settle_ms);
	if (!g_key_file_save_to_file(file, SETTINGS_FILE, &error))
		g_warning("Cannot save the settings: %s", error->message);
}

static gboolean on_set_property(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
				const char *property, GVariant *value, GError **error, gpointer user_data)
{
	if (strcmp(property, "OrientationSettleTime")) {
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_PROPERTY, "Unknown property %s", property);
		return FALSE;
	}
	guint32 ms = g_variant_get_uint32(value);
	if (ms < SETTLE_MS_MIN || ms > SETTLE_MS_MAX) {
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS, "Settle time %u ms out of %u-%u",
			    ms, SETTLE_MS_MIN, SETTLE_MS_MAX);
		return FALSE;
	}
	if (ms != self.settle_ms) {
		self.settle_ms = ms;
		save_settings();
	}
	return TRUE;
}

static const GDBusInterfaceVTable vtable = {on_method_call, on_get_property, NULL, {0}};
static const GDBusInterfaceVTable plinius_vtable = {on_method_call, on_get_property, on_set_property, {0}};

static void on_bus_acquired(GDBusConnection *bus, const char *name, gpointer user_data)
{
	GDBusNodeInfo *info = g_dbus_node_info_new_for_xml(introspection, NULL);
	self.bus = bus;
	self.object_id = g_dbus_connection_register_object(bus, "/net/hadess/SensorProxy", info->interfaces[0],
							   &vtable, NULL, NULL, NULL);
	g_dbus_connection_register_object(bus, "/net/hadess/SensorProxy", info->interfaces[1], &plinius_vtable,
					  NULL, NULL, NULL);
	g_dbus_connection_register_object(bus, "/net/hadess/SensorProxy/Compass", info->interfaces[2], &vtable,
					  NULL, NULL, NULL);
	g_dbus_connection_register_object(bus, "/net/hadess/SensorProxy/Gravity", info->interfaces[3], &vtable,
					  NULL, NULL, NULL);
	g_dbus_node_info_unref(info);
}

static void on_name_lost(GDBusConnection *bus, const char *name, gpointer user_data)
{
	g_warning("Lost or could not own %s", name);
	g_main_loop_quit(self.loop);
}

static gboolean on_signal(gpointer user_data)
{
	g_main_loop_quit(self.loop);
	return G_SOURCE_REMOVE;
}

int main(void)
{
	for (int id = 0; id < N_SENSORS; id++)
		self.sensors[id].claims = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, unwatch);
	if (!hal_connect()) {
		g_warning("Cannot initialize the sensors HAL");
		return 1;
	}
	load_settings();
	self.loop = g_main_loop_new(NULL, FALSE);
	self.reader = g_thread_new("sensor-events", reader_thread, NULL);
	guint owner = g_bus_own_name(G_BUS_TYPE_SYSTEM, "net.hadess.SensorProxy", G_BUS_NAME_OWNER_FLAGS_NONE,
				     on_bus_acquired, NULL, on_name_lost, NULL, NULL);
	g_unix_signal_add(SIGTERM, on_signal, NULL);
	g_unix_signal_add(SIGINT, on_signal, NULL);
	g_main_loop_run(self.loop);

	for (int id = 0; id < N_SENSORS; id++)
		if (self.sensors[id].handle >= 0 && g_hash_table_size(self.sensors[id].claims))
			hal_set_active(&self.sensors[id], FALSE);
	g_bus_unown_name(owner);
	g_atomic_int_set(&self.quit, 1);
	g_thread_join(self.reader);
	return 0;
}
