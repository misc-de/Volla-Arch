// SPDX-License-Identifier: MIT
// plinius-aml: Advanced Mobile Location (ETSI TS 103 625) for emergency calls.
//
// When the phone places an emergency call, the location goes to the emergency services as an
// SMS next to the call, as Android (ELS) and iOS do:
//
// 1. Trigger: oFono's VoiceCallManager.CallAdded for an outgoing call with Emergency=true
//    (oFono decides by the emergency numbers of the network, the SIM and its defaults 112/911).
//    Incoming calls never trigger, and nothing else can: the daemon has no D-Bus interface.
// 2. Country: the network's MCC selects a [Country <MCC>] group of /etc/plinius-aml/aml.conf.
//    Destination number and port are agreed between each country's PSAPs and the OS providers;
//    a country without them gets nothing, and the receiver is not even switched on.
// 3. Location: GNSS through gnss-nmea-bridge (connecting starts the receiver, whatever the
//    user's location setting says, as the standard asks). The message leaves as soon as a fix is
//    at least GoodAccuracy metres accurate, at the latest T1 seconds after the call started,
//    with the best fix so far or with "no location" (pm=N).
// 4. Message: version 1 of Annex A, packed into the GSM 7-bit alphabet and sent as a "data
//    SMS" (8-bit, application port in the user data header) through oFono's SendDataMessage on
//    the modem that carries the call. A data SMS is never stored as a sent message, so the
//    user sees nothing; the journal gets neither the location nor the identifiers.
//
// Not done: HTTPS transport (clause 6.4), Wi-Fi and cell positions, a second message later.
#include <gio/gio.h>
#include <gio/gunixsocketaddress.h>
#include <glib-unix.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#define CONFIG_FILE "/etc/plinius-aml/aml.conf"
#define NMEA_SOCKET "/run/gnss-nmea-bridge/nmea.sock"
#define BATTERY_DIR "/sys/class/power_supply/battery"

#define OFONO "org.ofono"
#define CALL_TIMEOUT_MS 3000
#define SEND_TIMEOUT_MS 30000

// 140 bytes of user data less 7 for the header with the 16-bit ports, 7 bits per character
#define AML_MAX_CHARS 152
// Error of one pseudorange (user equivalent range error) for a radius from the HDOP alone
#define UERE_M 5.0
// The radius is the distance root mean square: about 68 % of the fixes lie within it
#define CONFIDENCE 68
#define MAX_RADIUS 99999
#define SEND_ATTEMPTS 3
#define RETRY_S 5
#define RECONNECT_S 2

typedef struct {
	char *mcc;
	gboolean enabled;
	char *number;	      // SMS destination
	char *roaming_number; // instead while roaming (e.g. +<CC>112 as in GSMA NG.119)
	int port, source_port;
	gboolean legacy_packing; // bit order of the first AML implementations (note 1 of 6.3.2)
	gboolean partial_imei;
	char **numbers; // emergency numbers with AML; NULL: every one oFono knows
	int t1;
} Country;

typedef struct {
	int t1;		   // seconds from the call's start to the message at the latest
	int good_accuracy; // metres; a fix this good goes out at once
	int min_battery;   // percent; below it (and not charging) no GNSS
	GHashTable *countries; // MCC -> Country
} Config;

typedef struct {
	gboolean valid;
	double lat, lon;
	double hdop;		  // < 0: unknown
	double sigma_lat, sigma_lon; // metres from GST, < 0: unknown
	char utc[16];		  // hhmmss.ss of the sentence, to match GST to its GGA
	gint64 time_us;		  // wall clock when the fix came
} Fix;

typedef struct {
	char *call_path, *modem_path;
	Country *country;
	gint64 toc_us; // wall clock at the call's start
	Fix best, last;
	gboolean sending;
	int attempts;
	GSocketConnection *nmea;
	GDataInputStream *in;
	GCancellable *cancel;
	guint t1_id, retry_id, reconnect_id;
} Session;

static struct {
	GDBusConnection *bus;
	GMainLoop *loop;
	Config config;
	const char *nmea_socket;
	const char *battery_dir;
	Session *session;
} self = {.nmea_socket = NMEA_SOCKET, .battery_dir = BATTERY_DIR};

static void session_free(Session *s);

// ---- configuration ----

static void country_free(gpointer data)
{
	Country *c = data;

	g_free(c->mcc);
	g_free(c->number);
	g_free(c->roaming_number);
	g_strfreev(c->numbers);
	g_free(c);
}

static gboolean digits_only(const char *s, gsize min, gsize max)
{
	gsize n = s ? strlen(s) : 0;

	if (n < min || n > max)
		return FALSE;
	for (gsize i = 0; i < n; i++)
		if (!g_ascii_isdigit(s[i]))
			return FALSE;
	return TRUE;
}

// A number oFono takes for an SMS: digits with an optional leading +
static gboolean valid_number(const char *s)
{
	return s && digits_only(s[0] == '+' ? s + 1 : s, 1, 20);
}

static int key_int(GKeyFile *kf, const char *group, const char *key, int fallback, int min, int max)
{
	g_autoptr(GError) error = NULL;
	int v = g_key_file_get_integer(kf, group, key, &error);

	if (error) {
		if (!g_error_matches(error, G_KEY_FILE_ERROR, G_KEY_FILE_ERROR_KEY_NOT_FOUND))
			g_warning("%s: %s: %s", group, key, error->message);
		return fallback;
	}
	if (v < min || v > max) {
		g_warning("%s: %s=%d is outside %d..%d", group, key, v, min, max);
		return fallback;
	}
	return v;
}

static gboolean key_bool(GKeyFile *kf, const char *group, const char *key, gboolean fallback)
{
	g_autoptr(GError) error = NULL;
	gboolean v = g_key_file_get_boolean(kf, group, key, &error);

	if (error) {
		if (!g_error_matches(error, G_KEY_FILE_ERROR, G_KEY_FILE_ERROR_KEY_NOT_FOUND))
			g_warning("%s: %s: %s", group, key, error->message);
		return fallback;
	}
	return v;
}

static Country *country_load(GKeyFile *kf, const char *group, const char *mcc, int t1)
{
	Country *c = g_new0(Country, 1);
	g_autofree char *packing = g_key_file_get_string(kf, group, "Packing", NULL);
	g_autofree char *imei = g_key_file_get_string(kf, group, "IMEI", NULL);

	c->mcc = g_strdup(mcc);
	c->enabled = key_bool(kf, group, "Enabled", FALSE);
	c->number = g_key_file_get_string(kf, group, "Number", NULL);
	c->roaming_number = g_key_file_get_string(kf, group, "RoamingNumber", NULL);
	c->port = key_int(kf, group, "Port", 0, 1, 65535);
	c->source_port = key_int(kf, group, "SourcePort", c->port, 0, 65535);
	c->t1 = key_int(kf, group, "T1", t1, 1, 120);
	c->numbers = g_key_file_get_string_list(kf, group, "EmergencyNumbers", NULL, NULL);
	if (c->numbers && (!c->numbers[0] || !*c->numbers[0]))
		g_clear_pointer(&c->numbers, g_strfreev);

	if (packing && !g_str_equal(packing, "gsm") && !g_str_equal(packing, "legacy"))
		g_warning("%s: Packing=%s is neither gsm nor legacy, taking gsm", group, packing);
	c->legacy_packing = packing && g_str_equal(packing, "legacy");
	if (imei && !g_str_equal(imei, "full") && !g_str_equal(imei, "partial"))
		g_warning("%s: IMEI=%s is neither full nor partial, taking full", group, imei);
	c->partial_imei = imei && g_str_equal(imei, "partial");

	if (c->roaming_number && !valid_number(c->roaming_number)) {
		g_warning("%s: RoamingNumber is no phone number, ignored", group);
		g_clear_pointer(&c->roaming_number, g_free);
	}
	if (c->enabled && (!valid_number(c->number) || !c->port)) {
		g_warning("%s: enabled without a valid Number and Port, stays off", group);
		c->enabled = FALSE;
	}
	return c;
}

static void config_clear(Config *config)
{
	g_clear_pointer(&config->countries, g_hash_table_unref);
}

// A missing file is no error: no country has a destination then
static void config_load(Config *config, const char *path)
{
	g_autoptr(GKeyFile) kf = g_key_file_new();
	g_autoptr(GError) error = NULL;
	g_auto(GStrv) groups = NULL;

	config_clear(config);
	config->t1 = 20;
	config->good_accuracy = 30;
	config->min_battery = 3;
	config->countries = g_hash_table_new_full(g_str_hash, g_str_equal, NULL, country_free);

	if (!g_key_file_load_from_file(kf, path, G_KEY_FILE_NONE, &error)) {
		g_message("%s: %s; AML is off in every country", path, error->message);
		return;
	}
	config->t1 = key_int(kf, "General", "T1", config->t1, 1, 120);
	config->good_accuracy = key_int(kf, "General", "GoodAccuracy", config->good_accuracy, 1, 10000);
	config->min_battery = key_int(kf, "General", "MinBattery", config->min_battery, 0, 100);

	groups = g_key_file_get_groups(kf, NULL);
	for (char **g = groups; *g; g++) {
		const char *mcc;
		Country *c;

		if (!g_str_has_prefix(*g, "Country "))
			continue;
		mcc = *g + strlen("Country ");
		if (!digits_only(mcc, 3, 3)) {
			g_warning("[%s]: no MCC of three digits, ignored", *g);
			continue;
		}
		c = country_load(kf, *g, mcc, config->t1);
		g_hash_table_replace(config->countries, c->mcc, c);
	}
}

// ---- NMEA ----

// "$..*hh": the checksum is the XOR of everything between $ and *
static gboolean nmea_checksum_ok(const char *line)
{
	const char *star = strrchr(line, '*');
	guint8 sum = 0;
	unsigned int want;

	if (line[0] != '$' || !star || strlen(star) < 3 || sscanf(star + 1, "%2x", &want) != 1)
		return FALSE;
	for (const char *p = line + 1; p < star; p++)
		sum ^= (guint8)*p;
	return sum == want;
}

// ddmm.mmmm / dddmm.mmmm with N/S, E/W -> signed degrees
static gboolean nmea_coord(const char *value, const char *hemi, double *out)
{
	char *end;
	double raw = g_ascii_strtod(value, &end);
	double deg;

	if (!*value || *end || raw < 0 || !hemi || strlen(hemi) != 1)
		return FALSE;
	deg = floor(raw / 100);
	deg += (raw - deg * 100) / 60;
	switch (hemi[0]) {
	case 'S':
	case 'W':
		deg = -deg;
		/* fall through */
	case 'N':
	case 'E':
		*out = deg;
		return TRUE;
	default:
		return FALSE;
	}
}

static double nmea_number(const char *value)
{
	char *end;
	double v;

	if (!value || !*value)
		return -1;
	v = g_ascii_strtod(value, &end);
	return *end || v < 0 ? -1 : v;
}

// Radius in metres of the circle the fix lies in with CONFIDENCE %
static int fix_radius(const Fix *f)
{
	double r;

	if (f->sigma_lat >= 0 && f->sigma_lon >= 0)
		r = sqrt(f->sigma_lat * f->sigma_lat + f->sigma_lon * f->sigma_lon);
	else if (f->hdop > 0)
		r = f->hdop * UERE_M;
	else
		r = 10 * UERE_M; // no quality at all: as with an HDOP of 10
	return CLAMP((int)ceil(r), 1, MAX_RADIUS);
}

// Takes one sentence; TRUE when *fix got a (new or better described) position
static gboolean nmea_parse(const char *line, Fix *fix, gint64 now_us)
{
	g_autofree char *body = NULL;
	g_auto(GStrv) f = NULL;
	guint n;

	if (!nmea_checksum_ok(line))
		return FALSE;
	body = g_strndup(line + 1, strrchr(line, '*') - line - 1);
	f = g_strsplit(body, ",", -1);
	n = g_strv_length(f);
	// Talker (GP, GN, GL, GA, BD) and sentence type
	if (strlen(f[0]) != 5)
		return FALSE;

	// $--GGA,time,lat,N,lon,E,quality,satellites,hdop,altitude,M,separation,M,,
	if (g_str_equal(f[0] + 2, "GGA") && n >= 9) {
		Fix next = {.hdop = -1, .sigma_lat = -1, .sigma_lon = -1, .time_us = now_us};

		if (!g_strcmp0(f[6], "0") || !*f[6])
			return FALSE;
		if (!nmea_coord(f[2], f[3], &next.lat) || !nmea_coord(f[4], f[5], &next.lon))
			return FALSE;
		next.hdop = nmea_number(f[8]);
		g_strlcpy(next.utc, f[1], sizeof next.utc);
		// A GST of the same epoch may have come first
		if (fix->utc[0] && g_str_equal(fix->utc, next.utc) && !fix->valid) {
			next.sigma_lat = fix->sigma_lat;
			next.sigma_lon = fix->sigma_lon;
		}
		next.valid = TRUE;
		*fix = next;
		return TRUE;
	}
	// $--GST,time,rms,major,minor,orientation,sigma lat,sigma lon,sigma alt
	if (g_str_equal(f[0] + 2, "GST") && n >= 8) {
		double slat = nmea_number(f[6]), slon = nmea_number(f[7]);

		if (slat < 0 || slon < 0 || !*f[1])
			return FALSE;
		if (fix->valid && g_str_equal(fix->utc, f[1])) {
			fix->sigma_lat = slat;
			fix->sigma_lon = slon;
			return TRUE;
		}
		// Before its GGA: kept for it, not yet a position
		*fix = (Fix){.hdop = -1, .sigma_lat = slat, .sigma_lon = slon};
		g_strlcpy(fix->utc, f[1], sizeof fix->utc);
		return FALSE;
	}
	return FALSE;
}

// ---- the message (Annex A) ----

// Degrees with 5 decimals and a sign, without padding: +54.76397, -0.18305
static void format_degrees(char *buf, gsize len, double deg)
{
	char num[G_ASCII_DTOSTR_BUF_SIZE];
	double r = round(deg * 1e5) / 1e5;

	g_ascii_formatd(num, sizeof num, "%.5f", fabs(r));
	g_snprintf(buf, len, "%c%s", r < 0 ? '-' : '+', num);
}

static void format_utc(char *buf, gsize len, gint64 us)
{
	time_t t = us / G_USEC_PER_SEC;
	struct tm tm;

	gmtime_r(&t, &tm);
	strftime(buf, len, "%Y%m%d%H%M%S", &tm);
}

// The IMSI cut to its MCC and MNC, the subscriber part zeros
static char *partial_imsi(const char *imsi, gsize keep)
{
	gsize len;
	char *out;

	if (!digits_only(imsi, 6, 15))
		return g_strdup("000000000000000");
	len = strlen(imsi);
	out = g_strdup(imsi);
	for (gsize i = MIN(keep, len); i < len; i++)
		out[i] = '0';
	return out;
}

static char *imei_field(const char *imei, gboolean partial)
{
	char *out;

	if (!digits_only(imei, 14, 16))
		return g_strdup("000000000000000");
	out = g_strdup(imei);
	// The type allocation code (8 digits) names the model, the rest the handset
	if (partial)
		for (gsize i = 8; out[i]; i++)
			out[i] = '0';
	return out;
}

typedef struct {
	const Fix *fix;	  // NULL: no location
	gint64 top_us;	  // time of positioning (or of giving up)
	const char *imsi; // of the SIM, full
	gsize home_len;	  // digits of MCC + MNC in it
	const char *imei;
	gboolean partial_imei;
	const char *mcc, *mnc; // of the network
} Facts;

static gsize count_digits(gsize v)
{
	gsize n = 1;

	while (v >= 10) {
		v /= 10;
		n++;
	}
	return n;
}

// The text of the SMS; NULL if it would not fit
static char *aml_compose(const Facts *f)
{
	g_autoptr(GString) s = g_string_new("A\"ML=1;");
	g_autofree char *si = partial_imsi(f->imsi, f->home_len);
	g_autofree char *ei = imei_field(f->imei, f->partial_imei);
	char lt[32], lg[32], top[32];
	gsize base, ml;

	format_utc(top, sizeof top, f->top_us);
	if (f->fix) {
		format_degrees(lt, sizeof lt, f->fix->lat);
		format_degrees(lg, sizeof lg, f->fix->lon);
		g_string_append_printf(s, "lt=%s;lg=%s;rd=%d;top=%s;lc=%d;pm=G;", lt, lg, fix_radius(f->fix), top,
				       CONFIDENCE);
	} else {
		g_string_append_printf(s, "lt=+00.00000;lg=+000.00000;rd=N;top=%s;lc=0;pm=N;", top);
	}
	g_string_append_printf(s, "si=%s;ei=%s;mcc=%s;mnc=%s;ml=", si, ei,
			       digits_only(f->mcc, 3, 3) ? f->mcc : "000",
			       digits_only(f->mnc, 2, 3) ? f->mnc : "00");
	// The length counts the whole message, its own digits included
	base = s->len;
	ml = base + 1;
	while (base + count_digits(ml) != ml)
		ml++;
	g_string_append_printf(s, "%zu", ml);
	if (s->len > AML_MAX_CHARS)
		return NULL;
	return g_string_free(g_steal_pointer(&s), FALSE);
}

// Packs text into the GSM 7-bit default alphabet. Only characters whose GSM code is their ASCII
// code occur in an AML message (letters, digits, " + - . ; =), so the codes need no table.
// gsm: TS 23.038 6.1.2.1.1, the first character in the low bits of the first byte.
// legacy: the bit order of the first AML implementations, most significant bit first.
static GBytes *pack_gsm7(const char *text, gboolean legacy)
{
	gsize n = strlen(text);
	gsize len = (n * 7 + 7) / 8;
	guint8 *out = g_malloc0(MAX(len, 1));

	for (gsize i = 0; i < n; i++) {
		guint8 c = (guint8)text[i];
		gsize bit = i * 7;

		if (c > 0x7f || !(g_ascii_isalnum(c) || strchr("\"+-.;=", c))) {
			g_free(out);
			return NULL;
		}
		if (legacy) {
			for (int b = 6; b >= 0; b--, bit++)
				if (c & (1 << b))
					out[bit / 8] |= 0x80 >> (bit % 8);
		} else {
			out[bit / 8] |= (guint8)(c << (bit % 8));
			if (bit % 8 > 1)
				out[bit / 8 + 1] |= c >> (8 - bit % 8);
		}
	}
	return g_bytes_new_take(out, len);
}

// ---- oFono ----

static GVariant *ofono_properties(const char *path, const char *iface)
{
	g_autoptr(GError) error = NULL;
	GVariant *r = g_dbus_connection_call_sync(self.bus, OFONO, path, iface, "GetProperties", NULL,
						  G_VARIANT_TYPE("(a{sv})"), G_DBUS_CALL_FLAGS_NO_AUTO_START,
						  CALL_TIMEOUT_MS, NULL, &error);
	GVariant *dict;

	if (!r) {
		g_dbus_error_strip_remote_error(error);
		g_message("%s %s: %s", path, iface, error->message);
		return NULL;
	}
	dict = g_variant_get_child_value(r, 0);
	g_variant_unref(r);
	return dict;
}

static char *dict_string(GVariant *dict, const char *key)
{
	const char *v = NULL;

	if (dict && g_variant_lookup(dict, key, "&s", &v))
		return g_strdup(v);
	return NULL;
}

typedef struct {
	char *mcc, *mnc, *status;
} Network;

static void network_clear(Network *n)
{
	g_free(n->mcc);
	g_free(n->mnc);
	g_free(n->status);
}

static void network_read(const char *modem, Network *n)
{
	g_autoptr(GVariant) dict = ofono_properties(modem, OFONO ".NetworkRegistration");

	network_clear(n);
	n->mcc = dict_string(dict, "MobileCountryCode");
	n->mnc = dict_string(dict, "MobileNetworkCode");
	n->status = dict_string(dict, "Status");
}

// ---- battery ----

static char *battery_read(const char *name)
{
	g_autofree char *path = g_build_filename(self.battery_dir, name, NULL);
	char *text = NULL;

	if (!g_file_get_contents(path, &text, NULL, NULL))
		return NULL;
	return g_strstrip(text);
}

// Too low for GNSS: the call itself comes first (5.5). Unknown counts as enough.
static gboolean battery_low(void)
{
	g_autofree char *capacity = battery_read("capacity");
	g_autofree char *status = battery_read("status");

	if (!capacity || !digits_only(capacity, 1, 3))
		return FALSE;
	if (status && (g_str_equal(status, "Charging") || g_str_equal(status, "Full")))
		return FALSE;
	return atoi(capacity) < self.config.min_battery;
}

// ---- one emergency call ----

// Closing the socket switches the receiver off again (the bridge's last client gone)
static void nmea_close(Session *s)
{
	g_clear_handle_id(&s->reconnect_id, g_source_remove);
	g_cancellable_cancel(s->cancel);
	g_clear_object(&s->in);
	if (s->nmea) {
		g_io_stream_close(G_IO_STREAM(s->nmea), NULL, NULL);
		g_message("GNSS: receiver off");
	}
	g_clear_object(&s->nmea);
}

static void session_finish(Session *s, const char *why)
{
	g_message("AML %s", why);
	g_clear_handle_id(&s->t1_id, g_source_remove);
	g_clear_handle_id(&s->retry_id, g_source_remove);
	nmea_close(s);
	self.session = NULL;
	session_free(s);
}

static void session_free(Session *s)
{
	g_clear_object(&s->cancel);
	g_free(s->call_path);
	g_free(s->modem_path);
	g_free(s);
}

static void send_message(Session *s);

static gboolean on_retry(gpointer data)
{
	Session *s = data;

	s->retry_id = 0;
	send_message(s);
	return G_SOURCE_REMOVE;
}

static void on_sent(GObject *source, GAsyncResult *res, gpointer data)
{
	Session *s = data;
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) r = g_dbus_connection_call_finish(G_DBUS_CONNECTION(source), res, &error);

	if (r) {
		session_finish(s, "message handed to the modem");
		return;
	}
	g_dbus_error_strip_remote_error(error);
	g_warning("AML not sent (attempt %d of %d): %s", s->attempts, SEND_ATTEMPTS, error->message);
	if (s->attempts >= SEND_ATTEMPTS) {
		session_finish(s, "given up");
		return;
	}
	s->retry_id = g_timeout_add_seconds(RETRY_S, on_retry, s);
}

static void send_message(Session *s)
{
	g_autoptr(GVariant) modem = ofono_properties(s->modem_path, OFONO ".Modem");
	g_autoptr(GVariant) sim = ofono_properties(s->modem_path, OFONO ".SimManager");
	g_autofree char *imei = dict_string(modem, "Serial");
	g_autofree char *imsi = dict_string(sim, "SubscriberIdentity");
	g_autofree char *home_mnc = dict_string(sim, "MobileNetworkCode");
	g_autofree char *text = NULL;
	g_autoptr(GBytes) data = NULL;
	Network net = {0};
	const Country *c = s->country;
	const char *to;
	gint64 now = g_get_real_time();
	Facts facts;

	// The position is taken: GNSS off, also for a later attempt
	g_clear_handle_id(&s->t1_id, g_source_remove);
	s->sending = TRUE;
	nmea_close(s);
	s->attempts++;
	network_read(s->modem_path, &net);
	facts = (Facts){
		.fix = s->best.valid ? &s->best : NULL,
		.top_us = s->best.valid ? s->best.time_us : now,
		.imsi = imsi,
		.home_len = 3 + (home_mnc && strlen(home_mnc) == 3 ? 3 : 2),
		.imei = imei,
		.partial_imei = c->partial_imei,
		// The network the call is on; the country's when oFono lost it meanwhile
		.mcc = net.mcc ? net.mcc : c->mcc,
		.mnc = net.mnc ? net.mnc : home_mnc,
	};
	text = aml_compose(&facts);
	data = text ? pack_gsm7(text, c->legacy_packing) : NULL;
	to = g_strcmp0(net.status, "roaming") == 0 && c->roaming_number ? c->roaming_number : c->number;
	network_clear(&net);
	if (!data) {
		session_finish(s, "message could not be composed");
		return;
	}
	g_message("AML to country %s, %s%s after %" G_GINT64_FORMAT " s, %zu characters", c->mcc,
		  s->best.valid ? "GNSS fix" : "no location", to == c->roaming_number ? ", roaming number" : "",
		  (now - s->toc_us) / G_USEC_PER_SEC, strlen(text));
	g_dbus_connection_call(self.bus, OFONO, s->modem_path, OFONO ".MessageManager", "SendDataMessage",
			       g_variant_new("(s@ayiiu)", to,
					     g_variant_new_from_bytes(G_VARIANT_TYPE_BYTESTRING, data, TRUE),
					     c->source_port, c->port, 0),
			       G_VARIANT_TYPE("(o)"), G_DBUS_CALL_FLAGS_NO_AUTO_START, SEND_TIMEOUT_MS, NULL,
			       on_sent, s);
}

static gboolean on_t1(gpointer data)
{
	Session *s = data;

	s->t1_id = 0;
	send_message(s);
	return G_SOURCE_REMOVE;
}

static void nmea_connect(Session *s);

static gboolean on_reconnect(gpointer data)
{
	Session *s = data;

	s->reconnect_id = 0;
	nmea_connect(s);
	return G_SOURCE_REMOVE;
}

static void nmea_lost(Session *s, const char *why)
{
	g_message("GNSS: %s", why);
	g_clear_object(&s->in);
	g_clear_object(&s->nmea);
	if (!s->sending)
		s->reconnect_id = g_timeout_add_seconds(RECONNECT_S, on_reconnect, s);
}

static void on_line(GObject *source, GAsyncResult *res, gpointer data);

static void read_line(Session *s)
{
	g_data_input_stream_read_line_async(s->in, G_PRIORITY_DEFAULT, s->cancel, on_line, s);
}

static void on_line(GObject *source, GAsyncResult *res, gpointer data)
{
	g_autoptr(GError) error = NULL;
	g_autofree char *line = g_data_input_stream_read_line_finish(G_DATA_INPUT_STREAM(source), res, NULL,
								     &error);
	Session *s = data;

	if (g_error_matches(error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
		return; // the session is over (and maybe freed)
	if (!line) {
		nmea_lost(s, error ? error->message : "the bridge closed the stream");
		return;
	}
	g_strchomp(line);
	if (nmea_parse(line, &s->last, g_get_real_time()) && s->last.valid) {
		// The same epoch again is the same fix better described (GST after GGA)
		if (!s->best.valid || g_str_equal(s->best.utc, s->last.utc) ||
		    fix_radius(&s->last) <= fix_radius(&s->best))
			s->best = s->last;
		if (fix_radius(&s->best) <= self.config.good_accuracy) {
			send_message(s);
			return;
		}
	}
	read_line(s);
}

static void on_connected(GObject *source, GAsyncResult *res, gpointer data)
{
	g_autoptr(GError) error = NULL;
	GSocketConnection *conn = g_socket_client_connect_finish(G_SOCKET_CLIENT(source), res, &error);
	Session *s = data;

	if (g_error_matches(error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
		return;
	if (!conn) {
		nmea_lost(s, error->message);
		return;
	}
	if (s->sending) {
		g_io_stream_close(G_IO_STREAM(conn), NULL, NULL);
		g_object_unref(conn);
		return;
	}
	g_message("GNSS: receiver on");
	s->nmea = conn;
	s->in = g_data_input_stream_new(g_io_stream_get_input_stream(G_IO_STREAM(conn)));
	g_data_input_stream_set_newline_type(s->in, G_DATA_STREAM_NEWLINE_TYPE_ANY);
	read_line(s);
}

static void nmea_connect(Session *s)
{
	g_autoptr(GSocketClient) client = g_socket_client_new();
	g_autoptr(GSocketAddress) addr = g_unix_socket_address_new(self.nmea_socket);

	g_socket_client_connect_async(client, G_SOCKET_CONNECTABLE(addr), s->cancel, on_connected, s);
}

static gboolean wants_aml(const Country *c, const char *number)
{
	if (!c->numbers)
		return TRUE;
	for (char **n = c->numbers; *n; n++)
		if (!g_strcmp0(*n, number))
			return TRUE;
	return FALSE;
}

static void call_added(const char *path, GVariant *props)
{
	gboolean emergency = FALSE;
	const char *state = "", *number = "";
	g_autofree char *modem = NULL;
	Network net = {0};
	Session *s;
	Country *c;

	g_variant_lookup(props, "Emergency", "b", &emergency);
	g_variant_lookup(props, "State", "&s", &state);
	g_variant_lookup(props, "LineIdentification", "&s", &number);
	// Only calls the user makes: a call back from the emergency services is no trigger
	if (!emergency || g_str_equal(state, "incoming") || g_str_equal(state, "waiting"))
		return;
	if (self.session) {
		g_message("AML for %s already under way, nothing for %s", self.session->call_path, path);
		return;
	}
	modem = g_path_get_dirname(path);
	network_read(modem, &net);
	c = net.mcc ? g_hash_table_lookup(self.config.countries, net.mcc) : NULL;
	if (!c || !c->enabled) {
		g_message("Emergency call in country %s: no AML destination known there, nothing sent",
			  net.mcc ? net.mcc : "unknown");
		network_clear(&net);
		return;
	}
	network_clear(&net);
	if (!wants_aml(c, number)) {
		g_message("Emergency call in country %s to a number without AML", c->mcc);
		return;
	}

	s = g_new0(Session, 1);
	s->call_path = g_strdup(path);
	s->modem_path = g_steal_pointer(&modem);
	s->country = c;
	s->toc_us = g_get_real_time();
	s->cancel = g_cancellable_new();
	s->best.hdop = s->last.hdop = -1;
	self.session = s;
	g_message("Emergency call in country %s: AML within %d s", c->mcc, c->t1);
	if (battery_low()) {
		g_message("Battery low, the message goes without a location");
		send_message(s);
		return;
	}
	s->t1_id = g_timeout_add_seconds(c->t1, on_t1, s);
	nmea_connect(s);
}

static void on_call_added(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			  const char *signal, GVariant *params, gpointer data)
{
	const char *call;
	g_autoptr(GVariant) props = NULL;

	if (!g_variant_is_of_type(params, G_VARIANT_TYPE("(oa{sv})")))
		return;
	g_variant_get(params, "(&o@a{sv})", &call, &props);
	call_added(call, props);
}

static guint countries_enabled(void)
{
	GHashTableIter it;
	gpointer v;
	guint on = 0;

	g_hash_table_iter_init(&it, self.config.countries);
	while (g_hash_table_iter_next(&it, NULL, &v))
		on += ((Country *)v)->enabled;
	return on;
}

static gboolean on_signal(gpointer data)
{
	g_main_loop_quit(self.loop);
	return G_SOURCE_REMOVE;
}

int main(int argc, char **argv)
{
	g_autoptr(GError) error = NULL;
	g_autoptr(GOptionContext) ctx = NULL;
	g_autofree char *config = NULL, *nmea = NULL, *battery = NULL;
	GOptionEntry entries[] = {
		{"config", 0, 0, G_OPTION_ARG_FILENAME, &config, "Configuration (" CONFIG_FILE ")", "FILE"},
		{"nmea-socket", 0, 0, G_OPTION_ARG_FILENAME, &nmea, "NMEA socket (" NMEA_SOCKET ")", "PATH"},
		{"battery", 0, 0, G_OPTION_ARG_FILENAME, &battery, "Battery in sysfs (" BATTERY_DIR ")", "DIR"},
		{NULL}};

	ctx = g_option_context_new("- send the location of an emergency call (AML)");
	g_option_context_add_main_entries(ctx, entries, NULL);
	if (!g_option_context_parse(ctx, &argc, &argv, &error)) {
		g_printerr("%s\n", error->message);
		return 2;
	}
	if (nmea)
		self.nmea_socket = nmea;
	if (battery)
		self.battery_dir = battery;
	config_load(&self.config, config ? config : CONFIG_FILE);

	self.bus = g_bus_get_sync(G_BUS_TYPE_SYSTEM, NULL, &error);
	if (!self.bus) {
		g_printerr("System bus: %s\n", error->message);
		return 1;
	}
	g_dbus_connection_signal_subscribe(self.bus, OFONO, OFONO ".VoiceCallManager", "CallAdded", NULL, NULL,
					   G_DBUS_SIGNAL_FLAGS_NONE, on_call_added, NULL, NULL);
	g_message("Waiting for emergency calls, AML in %u countries", countries_enabled());

	self.loop = g_main_loop_new(NULL, FALSE);
	g_unix_signal_add(SIGTERM, on_signal, NULL);
	g_unix_signal_add(SIGINT, on_signal, NULL);
	g_main_loop_run(self.loop);

	if (self.session)
		session_finish(self.session, "cut short: the daemon stops");
	g_main_loop_unref(self.loop);
	g_object_unref(self.bus);
	config_clear(&self.config);
	return 0;
}
