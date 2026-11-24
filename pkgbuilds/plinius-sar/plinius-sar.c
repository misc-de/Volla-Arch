// SPDX-License-Identifier: MIT
// plinius-sar: lower the modem's transmit power while a body is near (SAR back-off).
//
// Android does this in the telephony framework, which Halium does not run. Here the capacitive
// SAR sensor (Awinic AW963xx on the SCP sensor hub) is read straight from the kernel's sensor hub
// manager (/dev/hf_manager, SENSOR_TYPE_SAR), so the single client the Android sensors HAL allows
// (sensor-proxy-bridge) is left alone. On a change the modem is told which SAR scene to use
// through the MediaTek radio extension (vendor.mediatek.hardware.mtkradioex.modem.IMtkRadioExModem).
//
// Two ways, chosen in the configuration (docs/MTKRADIOEX.md):
//   at         sendRequestStrings(["AT+ERFIDX=1,<scene>", ""]), the OEM hook EngineerMode uses.
//              The per-band power reduction of every scene is an NV item of the modem; the AP
//              only selects the index, -1 means normal power. Default, works on the Plinius.
//   indicator  sendSarIndicator(cmdType): the MIPC RIL turns it into SYS_SEND_SAR_IND_REQ with
//              one byte, 1 (near) or 0 (away). The Plinius modem rejects it (result 2).
//
//   plinius-sar              daemon
//   plinius-sar --monitor    print the raw SAR sensor events, send nothing
//   plinius-sar --scene N    send scene N (-1: normal power) to every slot once
//   SIGUSR1                  the daemon sets its current scene again
#include <errno.h>
#include <fcntl.h>
#include <gbinder.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define CONFIG_FILE "/etc/plinius-sar.conf"

/* ---- Kernel sensor hub manager (drivers/misc/mediatek/sensor/2.0/core/hf_sensor_io.h) ---- */

#define HF_MANAGER_DEVICE "/dev/hf_manager"
/* How long to wait for the device at the start: 150 × 0.2 s */
#define SENSOR_WAIT_TRIES 150
#define SENSOR_WAIT_US 200000
#define SENSOR_TYPE_SAR 72
enum { HF_SENSOR_DISABLE = 0, HF_SENSOR_ENABLE = 1 };
enum { DATA_ACTION = 0, FLUSH_ACTION = 1 };

struct hf_manager_batch {
	int64_t delay;
	int64_t latency;
} __attribute__((packed, aligned(4)));

struct hf_manager_cmd {
	uint8_t sensor_type;
	uint8_t action; /* action: 4 bits, down_sample: 1 bit */
	uint8_t length;
	uint8_t padding;
	int8_t data[48];
} __attribute__((packed, aligned(4)));

struct hf_manager_event {
	int64_t timestamp;
	uint8_t sensor_type;
	uint8_t accuracy;
	uint8_t action;
	uint8_t padding;
	int32_t word[16];
} __attribute__((packed, aligned(4)));

G_STATIC_ASSERT(sizeof(struct hf_manager_cmd) == 52);
G_STATIC_ASSERT(sizeof(struct hf_manager_event) == 76);

/* ---- MediaTek radio extension (docs/mtkradioex-aidl-codes.txt) ---- */

#define BINDER_DEVICE "/dev/binder"
#define MODEM_IFACE "vendor.mediatek.hardware.mtkradioex.modem.IMtkRadioExModem"
#define MODEM_RESPONSE_IFACE "vendor.mediatek.hardware.mtkradioex.modem.IMtkRadioExModemResponse"
#define MODEM_INDICATION_IFACE "vendor.mediatek.hardware.mtkradioex.modem.IMtkRadioExModemIndication"
/* IMtkRadioExModem */
enum {
	REQ_SEND_REQUEST_STRINGS = 11,
	REQ_SEND_SAR_INDICATOR = 12,
	REQ_RESPONSE_ACKNOWLEDGEMENT = 25,
	REQ_SET_RESPONSE_FUNCTIONS = 26,
};
/* IMtkRadioExModemResponse */
enum { RESP_SEND_REQUEST_STRINGS = 7, RESP_SEND_SAR_INDICATOR = 8 };
/* RadioResponseInfo.type */
#define RESPONSE_SOLICITED_ACK_EXP 2
/* AT+ERFIDX=<type>,<index>: type 1 is the SAR scene (0 is the antenna tuner) */
#define RF_INDEX_TYPE_SAR 1
#define SCENE_NORMAL (-1)
/* sendSarIndicator cmdType: 1 = near, 0 = away (MIPC RIL), 1 = AT+ERFIDX / 0 = AT+ERFTX (AT RIL) */
enum { SAR_INDICATOR_AWAY = 0, SAR_INDICATOR_NEAR = 1 };

enum method { METHOD_AT, METHOD_INDICATOR };

struct slot {
	char *name;
	GBinderRemoteObject *remote;
	GBinderClient *client;
	GBinderLocalObject *response;
	GBinderLocalObject *indication;
	gulong death_id;
	int scene_sent;      /* last scene the modem confirmed, G_MININT: none */
	int scene_requested; /* last scene sent, G_MININT: none */
	gint32 serial;
	guint retry_timer;
	guint answer_timer; /* the RIL owes us a response */
	guint resends;      /* after missing responses, in a row */
	guint failures;     /* rejections in a row, for quieter logging */
};

static struct {
	GMainLoop *loop;
	gboolean monitor;
	gboolean oneshot;
	int oneshot_scene;
	/* configuration */
	enum method method;
	int near_scene;
	int far_scene;
	int near_threshold;
	guint channel_mask; /* bit i: channel i counts */
	guint far_delay_ms;
	guint sampling_ms;
	guint refresh_s;
	guint retry_s;
	char **slot_names;
	/* sensor */
	int hf_fd;
	gboolean near;
	guint far_timer;
	int32_t last_words[3];
	/* modem */
	GBinderServiceManager *sm;
	GPtrArray *slots;
	guint retry_timer;
	guint refresh_timer;
	int outstanding; /* one-shot mode: answers still expected */
	gint32 next_serial;
	int exit_code;
} self = {
	.method = METHOD_AT,
	.near_scene = 0,
	.far_scene = SCENE_NORMAL,
	.near_threshold = 1,
	.channel_mask = 7,
	.far_delay_ms = 1000,
	.sampling_ms = 200,
	.refresh_s = 600,
	.retry_s = 30,
	.hf_fd = -1,
	.next_serial = 1,
};

static void slot_apply(struct slot *s);

static int current_scene(void)
{
	return self.near ? self.near_scene : self.far_scene;
}

/* ---- Configuration ---- */

static void load_config(const char *path)
{
	g_autoptr(GKeyFile) kf = g_key_file_new();
	g_autoptr(GError) error = NULL;
	if (!g_key_file_load_from_file(kf, path, G_KEY_FILE_NONE, &error)) {
		if (!g_error_matches(error, G_FILE_ERROR, G_FILE_ERROR_NOENT))
			g_warning("Cannot read %s: %s", path, error->message);
	} else {
		const char *g = "SAR";
		g_autofree char *method = g_key_file_get_string(kf, g, "Method", NULL);
		if (method && g_str_equal(method, "indicator"))
			self.method = METHOD_INDICATOR;
		else if (method && !g_str_equal(method, "at"))
			g_warning("Unknown Method '%s', using 'at'", method);
		if (g_key_file_has_key(kf, g, "NearScene", NULL))
			self.near_scene = g_key_file_get_integer(kf, g, "NearScene", NULL);
		if (g_key_file_has_key(kf, g, "FarScene", NULL))
			self.far_scene = g_key_file_get_integer(kf, g, "FarScene", NULL);
		if (g_key_file_has_key(kf, g, "NearThreshold", NULL))
			self.near_threshold = MAX(1, g_key_file_get_integer(kf, g, "NearThreshold", NULL));
		if (g_key_file_has_key(kf, g, "Channels", NULL)) {
			gsize n = 0;
			g_autofree gint *ch = g_key_file_get_integer_list(kf, g, "Channels", &n, NULL);
			self.channel_mask = 0;
			for (gsize i = 0; i < n; i++)
				if (ch[i] >= 0 && ch[i] < 3)
					self.channel_mask |= 1u << ch[i];
			if (!self.channel_mask) {
				g_warning("Channels names no channel 0..2, using all");
				self.channel_mask = 7;
			}
		}
		if (g_key_file_has_key(kf, g, "FarDelayMs", NULL))
			self.far_delay_ms = MAX(0, g_key_file_get_integer(kf, g, "FarDelayMs", NULL));
		if (g_key_file_has_key(kf, g, "SamplingMs", NULL))
			self.sampling_ms = CLAMP(g_key_file_get_integer(kf, g, "SamplingMs", NULL), 20, 5000);
		if (g_key_file_has_key(kf, g, "RefreshSeconds", NULL))
			self.refresh_s = MAX(0, g_key_file_get_integer(kf, g, "RefreshSeconds", NULL));
		if (g_key_file_has_key(kf, g, "RetrySeconds", NULL))
			self.retry_s = CLAMP(g_key_file_get_integer(kf, g, "RetrySeconds", NULL), 5, 3600);
		if (g_key_file_has_key(kf, g, "Slots", NULL))
			self.slot_names = g_key_file_get_string_list(kf, g, "Slots", NULL, NULL);
	}
	if (!self.slot_names || !self.slot_names[0]) {
		g_strfreev(self.slot_names);
		self.slot_names = g_strsplit("slot1;slot2", ";", -1);
	}
}

/* ---- Sensor ---- */

static gboolean hf_command(uint8_t action, const void *data, size_t length)
{
	struct hf_manager_cmd cmd = {.sensor_type = SENSOR_TYPE_SAR, .action = action, .length = length};
	if (data)
		memcpy(cmd.data, data, length);
	/* hf_manager_write() returns the driver's result, 0 on success, not the byte count */
	for (int attempt = 0;; attempt++) {
		if (write(self.hf_fd, &cmd, sizeof(cmd)) >= 0)
			return TRUE;
		/* The sensor hub refuses for a moment after the previous client went away */
		if (errno != EINVAL || attempt >= 10) {
			g_warning("SAR sensor command %u failed: %s", action, g_strerror(errno));
			return FALSE;
		}
		g_usleep(300000);
	}
}

static gboolean sensor_enable(gboolean on)
{
	if (!on)
		return hf_command(HF_SENSOR_DISABLE, NULL, 0);
	struct hf_manager_batch batch = {.delay = (int64_t)self.sampling_ms * 1000000, .latency = 0};
	return hf_command(HF_SENSOR_ENABLE, &batch, sizeof(batch));
}

static void set_near(gboolean near)
{
	if (near == self.near)
		return;
	self.near = near;
	g_message("Body %s (channels %d %d %d): SAR scene %d", near ? "near" : "away", self.last_words[0],
		  self.last_words[1], self.last_words[2], current_scene());
	for (guint i = 0; i < self.slots->len; i++)
		slot_apply(g_ptr_array_index(self.slots, i));
}

static gboolean on_far_delay(gpointer data)
{
	self.far_timer = 0;
	set_near(FALSE);
	return G_SOURCE_REMOVE;
}

/* Near as soon as a channel reports it; away only after the sensor has been quiet for the
 * configured delay, so a brief gap does not switch the power back and forth */
static void on_sar_event(const struct hf_manager_event *e)
{
	gboolean near = FALSE;
	for (int i = 0; i < 3; i++) {
		self.last_words[i] = e->word[i];
		if ((self.channel_mask & (1u << i)) && ABS(e->word[i]) >= self.near_threshold)
			near = TRUE;
	}
	if (self.monitor) {
		printf("%" G_GINT64_FORMAT ".%03d  channels %d %d %d  (%d %d %d)  accuracy %u  -> %s\n",
		       e->timestamp / 1000000000, (int)(e->timestamp / 1000000 % 1000), e->word[0], e->word[1],
		       e->word[2], e->word[3], e->word[4], e->word[5], e->accuracy, near ? "near" : "away");
		fflush(stdout);
		return;
	}
	if (near) {
		if (self.far_timer) {
			g_source_remove(self.far_timer);
			self.far_timer = 0;
		}
		set_near(TRUE);
	} else if (self.near && !self.far_timer) {
		if (self.far_delay_ms)
			self.far_timer = g_timeout_add(self.far_delay_ms, on_far_delay, NULL);
		else
			set_near(FALSE);
	}
}

static gboolean on_sensor_readable(gint fd, GIOCondition cond, gpointer data)
{
	struct hf_manager_event events[32];
	ssize_t n = read(fd, events, sizeof(events));
	if (n < 0) {
		if (errno == EAGAIN || errno == EINTR)
			return G_SOURCE_CONTINUE;
		g_warning("Reading the sensor hub failed: %s", g_strerror(errno));
		self.exit_code = 1;
		g_main_loop_quit(self.loop);
		return G_SOURCE_REMOVE;
	}
	for (ssize_t i = 0; i + (ssize_t)sizeof(events[0]) <= n; i += sizeof(events[0])) {
		const struct hf_manager_event *e = (const void *)((const char *)events + i);
		if (e->sensor_type == SENSOR_TYPE_SAR && e->action == DATA_ACTION)
			on_sar_event(e);
	}
	return G_SOURCE_CONTINUE;
}

static gboolean sensor_open(void)
{
	/* At boot this starts while the Android container is still loading the sensor hub's module */
	for (int i = 0; i < SENSOR_WAIT_TRIES; i++) {
		self.hf_fd = open(HF_MANAGER_DEVICE, O_RDWR | O_NONBLOCK | O_CLOEXEC);
		if (self.hf_fd >= 0 || errno != ENOENT)
			break;
		g_usleep(SENSOR_WAIT_US);
	}
	if (self.hf_fd < 0) {
		g_warning("Cannot open %s: %s", HF_MANAGER_DEVICE, g_strerror(errno));
		return FALSE;
	}
	if (!sensor_enable(TRUE))
		return FALSE;
	g_unix_fd_add(self.hf_fd, G_IO_IN, on_sensor_readable, NULL);
	g_message("SAR sensor on, every %u ms", self.sampling_ms);
	return TRUE;
}

/* ---- Modem ---- */

static const char *ril_error_name(gint32 error)
{
	switch (error) {
	case 1: return "RADIO_NOT_AVAILABLE";
	case 2: return "GENERIC_FAILURE";
	case 6: return "REQUEST_NOT_SUPPORTED";
	case 44: return "INVALID_ARGUMENTS";
	default: return "error";
	}
}

static void slot_disconnect(struct slot *s)
{
	if (s->remote && s->death_id)
		gbinder_remote_object_remove_handler(s->remote, s->death_id);
	s->death_id = 0;
	if (s->retry_timer)
		g_source_remove(s->retry_timer);
	s->retry_timer = 0;
	if (s->answer_timer)
		g_source_remove(s->answer_timer);
	s->answer_timer = 0;
	s->resends = 0;
	g_clear_pointer(&s->client, gbinder_client_unref);
	g_clear_pointer(&s->remote, gbinder_remote_object_unref);
	g_clear_pointer(&s->response, gbinder_local_object_unref);
	g_clear_pointer(&s->indication, gbinder_local_object_unref);
	s->scene_sent = G_MININT;
	s->scene_requested = G_MININT;
	s->failures = 0;
}

static void slot_acknowledge(struct slot *s)
{
	GBinderLocalRequest *req = gbinder_client_new_request2(s->client, REQ_RESPONSE_ACKNOWLEDGEMENT);
	gbinder_client_transact(s->client, REQ_RESPONSE_ACKNOWLEDGEMENT, GBINDER_TX_FLAG_ONEWAY, req, NULL, NULL, NULL);
	gbinder_local_request_unref(req);
}

static gboolean on_slot_retry(gpointer data)
{
	struct slot *s = data;
	s->retry_timer = 0;
	slot_apply(s);
	return G_SOURCE_REMOVE;
}

static void slot_done(struct slot *s, gboolean ok, const char *detail)
{
	if (ok) {
		s->scene_sent = s->scene_requested;
		if (s->failures)
			g_message("%s: SAR scene %d set after %u failures", s->name, s->scene_sent, s->failures);
		else
			g_message("%s: SAR scene %d set", s->name, s->scene_sent);
		s->failures = 0;
	} else {
		/* The radio may be off (no SIM, flight mode): say so once, then keep trying quietly */
		if (s->failures++ == 0)
			g_warning("%s: SAR scene %d rejected: %s; retrying every %u s", s->name, s->scene_requested, detail,
				  self.retry_s);
		else
			g_debug("%s: SAR scene %d rejected: %s", s->name, s->scene_requested, detail);
		if (!self.oneshot && !s->retry_timer)
			s->retry_timer = g_timeout_add_seconds(self.retry_s, on_slot_retry, s);
	}
	if (self.oneshot) {
		if (!ok)
			self.exit_code = 1;
		if (--self.outstanding <= 0)
			g_main_loop_quit(self.loop);
	}
}

/* IMtkRadioExModemResponse. Both answers start with a RadioResponseInfo (non-null marker, size,
 * type, serial, error); sendRequestStringsResponse adds the modem's lines */
static GBinderLocalReply *on_response(GBinderLocalObject *obj, GBinderRemoteRequest *req, guint code, guint flags,
				      int *status, void *user_data)
{
	struct slot *s = user_data;
	*status = GBINDER_STATUS_OK;
	if (code != RESP_SEND_SAR_INDICATOR && code != RESP_SEND_REQUEST_STRINGS)
		return NULL;
	GBinderReader r;
	gint32 non_null = 0, size = 0, type = 0, serial = 0, error = -1;
	gbinder_remote_request_init_reader(req, &r);
	if (!gbinder_reader_read_int32(&r, &non_null) || !gbinder_reader_read_int32(&r, &size) ||
	    !gbinder_reader_read_int32(&r, &type) || !gbinder_reader_read_int32(&r, &serial) ||
	    !gbinder_reader_read_int32(&r, &error)) {
		g_warning("%s: unreadable response %u", s->name, code);
		return NULL;
	}
	if (type == RESPONSE_SOLICITED_ACK_EXP)
		slot_acknowledge(s);
	if (serial != s->serial)
		return NULL;
	if (s->answer_timer)
		g_source_remove(s->answer_timer);
	s->answer_timer = 0;
	s->resends = 0;
	g_autofree char *lines = NULL;
	if (code == RESP_SEND_REQUEST_STRINGS) {
		gint32 count = 0;
		GString *all = g_string_new(NULL);
		if (gbinder_reader_read_int32(&r, &count))
			for (gint32 i = 0; i < count && i < 8; i++) {
				char *line = gbinder_reader_read_string16(&r);
				if (line && *line)
					g_string_append_printf(all, "%s%s", all->len ? " | " : "", g_strstrip(line));
				g_free(line);
			}
		lines = g_string_free(all, FALSE);
	}
	g_autofree char *detail = g_strdup_printf("RIL %s (%d)%s%s", ril_error_name(error), error,
						  lines && *lines ? ", modem: " : "", lines && *lines ? lines : "");
	slot_done(s, error == 0, detail);
	return NULL;
}

static GBinderLocalReply *on_indication(GBinderLocalObject *obj, GBinderRemoteRequest *req, guint code, guint flags,
					int *status, void *user_data)
{
	*status = GBINDER_STATUS_OK;
	return NULL;
}

/* setResponseFunctionsMtk(response, indication): where the RIL sends its answers. Whoever calls
 * it last gets them, so this is repeated when an answer fails to come (another client such as
 * `plinius-sar --scene`, or a RIL that started over) */
static gboolean slot_register(struct slot *s)
{
	int status = 0;
	GBinderLocalRequest *req = gbinder_client_new_request2(s->client, REQ_SET_RESPONSE_FUNCTIONS);
	GBinderWriter w;
	gbinder_local_request_init_writer(req, &w);
	gbinder_writer_append_local_object(&w, s->response);
	gbinder_writer_append_local_object(&w, s->indication);
	GBinderRemoteReply *reply = gbinder_client_transact_sync_reply(s->client, REQ_SET_RESPONSE_FUNCTIONS, req, &status);
	gbinder_remote_reply_unref(reply);
	gbinder_local_request_unref(req);
	if (status != GBINDER_STATUS_OK)
		g_warning("%s: setResponseFunctionsMtk failed (%d)", s->name, status);
	return status == GBINDER_STATUS_OK;
}

#define ANSWER_TIMEOUT_S 5
#define MAX_RESENDS 3

static void slot_send_scene(struct slot *s, int scene);

static gboolean on_answer_timeout(gpointer data)
{
	struct slot *s = data;
	s->answer_timer = 0;
	if (self.oneshot) {
		g_warning("%s: no answer from the RIL within %d s", s->name, ANSWER_TIMEOUT_S);
		self.exit_code = 1;
		if (--self.outstanding <= 0)
			g_main_loop_quit(self.loop);
		return G_SOURCE_REMOVE;
	}
	if (s->resends++ < MAX_RESENDS) {
		g_message("%s: no answer within %d s, registering again and resending", s->name, ANSWER_TIMEOUT_S);
		if (slot_register(s))
			slot_send_scene(s, s->scene_requested);
		else
			s->retry_timer = g_timeout_add_seconds(self.retry_s, on_slot_retry, s);
	} else {
		g_warning("%s: the RIL does not answer; trying again in %u s", s->name, self.retry_s);
		s->resends = 0;
		s->retry_timer = g_timeout_add_seconds(self.retry_s, on_slot_retry, s);
	}
	return G_SOURCE_REMOVE;
}

/* at: sendRequestStrings(serial, ["AT+ERFIDX=1,<scene>", ""], clientId), the OEM hook that runs
 * an AT command; the second string is the expected answer prefix, none here.
 * indicator: sendSarIndicator(serial, cmdType, parameter, clientId) */
static void slot_send_scene(struct slot *s, int scene)
{
	GBinderLocalRequest *req;
	GBinderWriter w;
	s->serial = self.next_serial++;
	s->scene_requested = scene;
	if (self.method == METHOD_AT) {
		g_autofree char *cmd = g_strdup_printf("AT+ERFIDX=%d,%d", RF_INDEX_TYPE_SAR, scene);
		req = gbinder_client_new_request2(s->client, REQ_SEND_REQUEST_STRINGS);
		gbinder_local_request_init_writer(req, &w);
		gbinder_writer_append_int32(&w, s->serial);
		gbinder_writer_append_int32(&w, 2);
		gbinder_writer_append_string16(&w, cmd);
		gbinder_writer_append_string16(&w, "");
		gbinder_writer_append_int32(&w, 0);
		if (!gbinder_client_transact(s->client, REQ_SEND_REQUEST_STRINGS, GBINDER_TX_FLAG_ONEWAY, req, NULL, NULL,
					     NULL))
			g_warning("%s: cannot send %s", s->name, cmd);
		else
			g_debug("%s: %s sent (serial %d)", s->name, cmd, s->serial);
	} else {
		int indicator = scene == self.far_scene ? SAR_INDICATOR_AWAY : SAR_INDICATOR_NEAR;
		g_autofree char *parameter = g_strdup_printf("%d,%d", RF_INDEX_TYPE_SAR, scene);
		req = gbinder_client_new_request2(s->client, REQ_SEND_SAR_INDICATOR);
		gbinder_local_request_init_writer(req, &w);
		gbinder_writer_append_int32(&w, s->serial);
		gbinder_writer_append_int32(&w, indicator);
		gbinder_writer_append_string16(&w, parameter);
		gbinder_writer_append_int32(&w, 0);
		if (!gbinder_client_transact(s->client, REQ_SEND_SAR_INDICATOR, GBINDER_TX_FLAG_ONEWAY, req, NULL, NULL, NULL))
			g_warning("%s: cannot send SAR indicator %d", s->name, indicator);
		else
			g_debug("%s: SAR indicator %d sent (serial %d)", s->name, indicator, s->serial);
	}
	gbinder_local_request_unref(req);
	if (s->answer_timer)
		g_source_remove(s->answer_timer);
	s->answer_timer = g_timeout_add_seconds(ANSWER_TIMEOUT_S, on_answer_timeout, s);
}

/* The scene the modem ends up with: while an answer is owed that is the one sent last, not the
 * last one confirmed (near again while 'away' is unanswered must still send the near scene) */
static int slot_scene_due(const struct slot *s)
{
	return s->answer_timer ? s->scene_requested : s->scene_sent;
}

static void slot_apply(struct slot *s)
{
	if (!s->client)
		return;
	int scene = self.oneshot ? self.oneshot_scene : current_scene();
	if (scene == slot_scene_due(s))
		return;
	slot_send_scene(s, scene);
}

static gboolean slots_connect(gpointer data);

static void on_slot_died(GBinderRemoteObject *obj, void *user_data)
{
	struct slot *s = user_data;
	g_warning("%s: radio extension gone, waiting for it", s->name);
	slot_disconnect(s);
	if (!self.retry_timer)
		self.retry_timer = g_timeout_add_seconds(2, slots_connect, NULL);
}

static gboolean slot_connect(struct slot *s)
{
	int status = 0;
	g_autofree char *fqname = g_strconcat(MODEM_IFACE, "/", s->name, NULL);
	GBinderRemoteObject *remote = gbinder_servicemanager_get_service_sync(self.sm, fqname, &status);
	if (!remote)
		return FALSE;
	s->remote = gbinder_remote_object_ref(remote);
	s->death_id = gbinder_remote_object_add_death_handler(s->remote, on_slot_died, s);
	s->client = gbinder_client_new(s->remote, MODEM_IFACE);
	s->response = gbinder_servicemanager_new_local_object(self.sm, MODEM_RESPONSE_IFACE, on_response, s);
	s->indication = gbinder_servicemanager_new_local_object(self.sm, MODEM_INDICATION_IFACE, on_indication, s);
	gbinder_local_object_set_stability(s->response, GBINDER_STABILITY_VENDOR);
	gbinder_local_object_set_stability(s->indication, GBINDER_STABILITY_VENDOR);

	if (!slot_register(s)) {
		slot_disconnect(s);
		return FALSE;
	}
	g_message("%s: connected to the radio extension", s->name);
	slot_apply(s);
	return TRUE;
}

/* Connects every slot that is not connected yet; keeps trying while one is missing (the Android
 * container may still be starting, or the RIL restarted) */
static gboolean slots_connect(gpointer data)
{
	gboolean missing = FALSE;
	for (guint i = 0; i < self.slots->len; i++) {
		struct slot *s = g_ptr_array_index(self.slots, i);
		if (!s->client && !slot_connect(s))
			missing = TRUE;
	}
	self.retry_timer = missing ? g_timeout_add_seconds(5, slots_connect, NULL) : 0;
	return G_SOURCE_REMOVE;
}

/* Sends the current scene again now and then: a modem reset forgets it, and the RIL may not
 * tell us */
static gboolean on_refresh(gpointer data)
{
	for (guint i = 0; i < self.slots->len; i++) {
		struct slot *s = g_ptr_array_index(self.slots, i);
		s->scene_sent = G_MININT;
		s->scene_requested = G_MININT;
		slot_apply(s);
	}
	return G_SOURCE_CONTINUE;
}

static gboolean modem_init(void)
{
	self.sm = gbinder_servicemanager_new(BINDER_DEVICE);
	if (!self.sm || !gbinder_servicemanager_wait(self.sm, 30000)) {
		g_warning("No binder service manager on %s", BINDER_DEVICE);
		return FALSE;
	}
	self.slots = g_ptr_array_new();
	for (char **name = self.slot_names; *name; name++) {
		struct slot *s = g_new0(struct slot, 1);
		s->name = g_strdup(*name);
		s->scene_sent = G_MININT;
		s->scene_requested = G_MININT;
		g_ptr_array_add(self.slots, s);
	}
	slots_connect(NULL);
	if (self.refresh_s && !self.oneshot)
		self.refresh_timer = g_timeout_add_seconds(self.refresh_s, on_refresh, NULL);
	return TRUE;
}

/* ---- Main ---- */

static gboolean on_signal(gpointer data)
{
	g_main_loop_quit(self.loop);
	return G_SOURCE_REMOVE;
}

static gboolean on_oneshot_timeout(gpointer data)
{
	g_warning("No answer from the modem within 5 s");
	self.exit_code = 1;
	g_main_loop_quit(self.loop);
	return G_SOURCE_REMOVE;
}

int main(int argc, char **argv)
{
	const char *config = CONFIG_FILE;
	int scene = G_MININT; /* sentinel: --scene not given */
	GOptionEntry entries[] = {
		{"monitor", 'm', 0, G_OPTION_ARG_NONE, &self.monitor, "Print the SAR sensor events, send nothing", NULL},
		{"scene", 's', 0, G_OPTION_ARG_INT, &scene, "Send this SAR scene once and exit (-1: normal power)", "N"},
		{"config", 'c', 0, G_OPTION_ARG_FILENAME, &config, "Configuration file (default " CONFIG_FILE ")", "FILE"},
		{NULL},
	};
	g_autoptr(GOptionContext) ctx = g_option_context_new("- SAR back-off of the modem's transmit power");
	g_option_context_add_main_entries(ctx, entries, NULL);
	g_autoptr(GError) error = NULL;
	if (!g_option_context_parse(ctx, &argc, &argv, &error)) {
		g_printerr("%s\n", error->message);
		return 2;
	}
	gboolean scene_given = scene != G_MININT;
	load_config(config);
	self.loop = g_main_loop_new(NULL, FALSE);
	g_unix_signal_add(SIGINT, on_signal, NULL);
	g_unix_signal_add(SIGTERM, on_signal, NULL);
	/* SIGUSR1: set the current scene again right now */
	g_unix_signal_add(SIGUSR1, on_refresh, NULL);

	if (self.monitor) {
		if (!sensor_open())
			return 1;
		g_main_loop_run(self.loop);
		sensor_enable(FALSE);
		return 0;
	}
	if (scene_given) {
		self.oneshot = TRUE;
		self.oneshot_scene = scene;
		self.outstanding = g_strv_length(self.slot_names);
		if (!modem_init())
			return 1;
		g_timeout_add_seconds(5, on_oneshot_timeout, NULL);
		g_main_loop_run(self.loop);
		return self.exit_code;
	}

	if (!modem_init())
		return 1;
	if (!sensor_open())
		return 1;
	g_main_loop_run(self.loop);
	/* Leave the modem at normal power when we stop */
	sensor_enable(FALSE);
	self.near = FALSE;
	for (guint i = 0; i < self.slots->len; i++) {
		struct slot *s = g_ptr_array_index(self.slots, i);
		if (s->client && (s->scene_sent != self.far_scene || slot_scene_due(s) != self.far_scene))
			slot_send_scene(s, self.far_scene);
	}
	return self.exit_code;
}
