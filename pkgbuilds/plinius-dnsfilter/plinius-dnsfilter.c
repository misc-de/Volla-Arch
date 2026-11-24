// SPDX-License-Identifier: MIT
// plinius-dnsfilter: the DNS filter of the Volla Phone Plinius (Settings > Network & VPN >
// DNS Filter). A small DNS server on the phone that answers for every connection:
//
// - The phone itself: /etc/resolv.conf links to /run/plinius-dns/resolv.conf, which links to
//   our stub file (nameserver 127.0.0.1) while the filter is on and to NetworkManager's file
//   when it is off or the service is not running. NetworkManager (rc-manager=symlink) leaves a
//   symlink alone and keeps writing its own file, so nothing breaks when we are gone.
// - Hotspot and USB clients: port 53 arriving on ap+ and usb+ is redirected (iptables-legacy,
//   the GKI kernel has no nf_tables) to our port 5300, so their own dnsmasq stays as it is and
//   even clients with a fixed DNS server (8.8.8.8) are filtered. We see their real addresses.
// - Apps of the user with a fixed DNS server: port 53 of the session users is redirected to us.
//   Only their uids: Android shares the network namespace and its IMS lookups stay untouched.
// - Optionally DNS over TLS/QUIC (port 853) of the user and the clients is refused, so they
//   fall back to plain DNS, which we see.
//
// Names are checked against the user's own rules for the app that asked ("...$app=<key>", the key
// as appid.h makes it), then against the own rules for all apps, then against the subscribed lists
// (filter.h); a block answers 0.0.0.0/:: (or NXDOMAIN/REFUSED). CNAMEs in answers are checked
// too (trackers behind a CNAME of the site's own domain). Allowed names go to the network's DNS
// servers (from NetworkManager, per interface, with its routing domains) or to servers the user
// set: plain, DNS over TLS (tls://) or DNS over HTTPS (https://). Their host names are looked up
// through the network's servers, never through ourselves.
//
// Settings, lists, own rules, statistics and the query log go through org.plinius.DnsFilter on
// the system bus. Only root and the user of the active session may use it, and that user only if
// it is an administrator (group wheel): the servers, lists and rules act for every account on the
// phone, and the log and statistics hold the names every account looked up (attack review
// 2026-11-18, L15).
//
// Without a settings file the filter is off; a fresh install of the package writes one with the
// filter on (plinius-dnsfilter.install). While it is off, the service only runs when Settings asks
// (D-Bus activation) and leaves after a while.
/* struct in6_pktinfo */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <ifaddrs.h>
#include <malloc.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <glib/gstdio.h>
#include <libsoup/soup.h>
#include <net/if.h>
#include <netinet/in.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <grp.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

#include "appid.h"
#include "dnsmsg.h"
#include "filter.h"

#define BUS_NAME "org.plinius.DnsFilter"
#define OBJECT_PATH "/org/plinius/DnsFilter"
#define INTERFACE "org.plinius.DnsFilter"

#define DEFAULT_STATE_DIR "/var/lib/plinius-dnsfilter"
#define DEFAULT_LINK_DIR "/run/plinius-dns"
#define NM_RESOLV "/run/NetworkManager/resolv.conf"

/* Our own port for redirected DNS of hotspot and USB clients */
#define SHARED_PORT 5300

/* Upstream: wait per server, and for the whole question */
#define UDP_TIMEOUT_MS 1500
#define STREAM_TIMEOUT_MS 3000
#define DEADLINE_MS 8000
/* EDNS buffer size towards servers (the DNS flag day 2020 value) */
#define UPSTREAM_UDP_SIZE 1232
/* TTL of our block answers */
#define BLOCK_TTL 10
/* Cache: entries, and the longest a negative answer is kept */
#define CACHE_MAX 4096
#define CACHE_NEGATIVE_MAX 300
#define CACHE_TTL_MAX 86400
/* Open connections to DoT servers close after this long without questions */
#define STREAM_IDLE_S 20
/* A TCP client gets this long between questions */
#define TCP_CLIENT_IDLE_S 10
/* TCP clients of the phone (loopback), of hotspot and USB together, and per hotspot/USB device;
 * the phone keeps its own share, so shared clients can't starve it */
#define TCP_CLIENTS_MAX 64
#define TCP_SHARED_MAX 32
#define TCP_PER_PEER_MAX 8
/* Longest a TCP connection is kept, however busy: with keepalive questions every few seconds, 64
 * connections of one app held all places for ever, and DNS over TCP (truncated answers) failed
 * for every other. When all places are taken, a new connection also ends the oldest one */
#define TCP_CLIENT_LIFETIME_S 120
/* Per TCP connection: questions in flight, and queued answer bytes at which we stop reading.
 * A client that sends without reading gets nothing more read from it, so its answers can't
 * pile up in our memory; beyond TCP_QUEUE_MAX (a single answer can't get there after the
 * pause) it is cut off. */
#define TCP_PENDING_MAX 16
#define TCP_QUEUE_PAUSE (64 * 1024)
#define TCP_QUEUE_MAX (256 * 1024)
/* Requests in flight; beyond that new questions get SERVFAIL */
#define REQUESTS_MAX 1024
/* ... and of one app (or hotspot/USB device): random names under a domain whose servers don't
 * answer, about 130 a second, took all 1024 places and SERVFAIL for the whole phone */
#define REQUESTS_PER_ASKER 256
/* Lists: largest download and most rules (the biggest catalog list, HaGeZi TIF, has well
 * under a million; compiling 2 million takes ~120 MiB, below the unit's MemoryMax), first check
 * after start, retry after a failure */
#define LIST_MAX_BYTES (32 * 1024 * 1024)
#define LIST_MAX_RULES 2000000
#define LIST_FIRST_CHECK_S 30
#define LIST_RETRY_S (30 * 60)
/* With the filter off and nobody watching, the service leaves after this */
#define IDLE_EXIT_S 120
/* Top lists: entries kept per day and table */
#define TOP_KEEP 300
/* Blocked names kept per hour (Most Blocked): each hour of StatsDays has its own table, so it is
 * kept small. Random names under a blocked domain asked every few seconds made the statistics
 * grow by about 16 MB a day until saving them ran out of memory (MemoryMax) */
#define HOUR_BLOCKED_KEEP 50
/* All tables of the statistics together, at about what they take in the file: beyond it the
 * oldest days lose their names (their counts stay). A file larger than STATS_FILE_MAX is not read */
#define STATS_MAX_BYTES (8 * 1024 * 1024)
#define STATS_FILE_MAX (16 * 1024 * 1024)
/* Longest name in the tables: a DNS name has at most 253 characters as text */
#define STATS_NAME_MAX 253
/* Who asked which name ("<who>\n<name>"), per day: for a name's apps and devices, an app's or a
 * device's names */
#define PAIRS_KEEP 1000
/* The end of a rule that holds for one app only */
#define APP_MODIFIER "$app="
/* Statistics are written this often */
#define SAVE_INTERVAL_S 3600
/* The live log goes out in batches */
#define LOG_BATCH_MS 500
/* Largest DNS over HTTPS answer we read (a DNS message can't be longer) */
#define DOH_MAX_BYTES 65535

typedef enum {
	STATUS_PASSED,		/* no rule, answered by the server */
	STATUS_ALLOWED_RULE,	/* own exception */
	STATUS_ALLOWED_LIST,	/* exception of a list */
	STATUS_ALLOWED_SYSTEM,	/* the phone needs it (connectivity check, time, updates) */
	STATUS_BLOCKED_LIST,
	STATUS_BLOCKED_RULE,
	STATUS_UNFILTERED,	/* the filter is paused */
	STATUS_FAILED,		/* no server answered */
	STATUS_BLOCKED_SPECIAL, /* Firefox' canary domain: no DNS over HTTPS behind our back */
} Status;

static gboolean status_blocked(Status s)
{
	return s == STATUS_BLOCKED_LIST || s == STATUS_BLOCKED_RULE || s == STATUS_BLOCKED_SPECIAL;
}

typedef enum {
	CLIENT_PHONE,
	CLIENT_HOTSPOT,
	CLIENT_USB,
	CLIENT_INTERNAL,
} ClientKind;

static const char *const client_kinds[] = {"phone", "hotspot", "usb", "internal"};

/* ---- Lists ---- */

typedef struct {
	const char *id;
	const char *name;
	const char *url;
	const char *description;
} CatalogEntry;

/* Lists one can pick without typing an address. HaGeZi Normal is the default one. */
static const CatalogEntry catalog[] = {
	{"hagezi-normal", "HaGeZi Multi Normal", "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/multi.txt",
	 "Ads, tracking, metrics, telemetry, phishing, malware; rarely breaks anything"},
	{"hagezi-light", "HaGeZi Multi Light", "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/light.txt",
	 "The basics, for a phone that must never miss anything"},
	{"hagezi-pro", "HaGeZi Multi Pro", "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/pro.txt",
	 "Stricter; now and then a site needs an exception"},
	{"hagezi-pro-plus", "HaGeZi Multi Pro++", "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/pro.plus.txt",
	 "Aggressive; expect exceptions"},
	{"hagezi-tif", "HaGeZi Threat Intelligence Feeds", "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/tif.txt",
	 "Malware, phishing, scams and command and control servers"},
	{"hagezi-bypass", "HaGeZi DoH/VPN/TOR/Proxy Bypass", "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/doh-vpn-proxy-bypass.txt",
	 "Encrypted DNS and VPN services apps could use to get past the filter"},
	{"oisd-small", "OISD Small", "https://small.oisd.nl/", "Ads and tracking, made not to break anything"},
	{"oisd-big", "OISD Big", "https://big.oisd.nl/", "Ads, tracking, malware and more"},
	{"adguard-dns", "AdGuard DNS filter", "https://adguardteam.github.io/AdGuardSDNSFilter/Filters/filter.txt",
	 "Ads and tracking, put together from several lists"},
	{"stevenblack", "StevenBlack Unified Hosts", "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
	 "Ads and malware, in hosts format"},
};
#define DEFAULT_LIST "hagezi-normal"

typedef struct {
	char *id;
	char *name;
	char *url;
	gboolean enabled;
	char *etag;
	char *last_modified;
	gint64 updated;	 /* last successful download, unix seconds */
	gint64 checked;	 /* last attempt */
	guint rules;
	guint unsupported;
	char *error;
	gboolean downloading;
	FilterSet *set;
} List;

/* ---- Upstream servers ---- */

typedef enum {
	PROTO_UDP, /* plain DNS, TCP when the answer is truncated */
	PROTO_TLS,
	PROTO_HTTPS,
} Proto;

typedef struct Stream Stream;

typedef struct {
	int ref;
	char *spec; /* as the user wrote it, or the address for network servers */
	Proto proto;
	GSocketAddress *addr; /* UDP/TCP */
	char *host;	      /* TLS: server name and port */
	guint16 port;
	char *url;   /* HTTPS */
	char *iface; /* network servers are asked through their interface */
	Stream *stream;
	/* For ordering: consecutive failures and when the last one was */
	guint failures;
	gint64 failed_at;
} Upstream;

/* One interface's DNS from NetworkManager */
typedef struct {
	char *iface;
	int priority;
	gboolean vpn;
	char **domains;
	GPtrArray *servers; /* Upstream* */
} NetLink;

/* ---- Requests ---- */

typedef struct TcpClient TcpClient;
typedef struct Request Request;
typedef void (*InternalDone)(Request *r, const guint8 *answer, gsize len, gpointer data);

typedef struct {
	Status status;
	char *reason; /* list name or rule */
} Decision;

struct Request {
	int ref;
	gboolean finished;
	ClientKind kind;
	/* UDP clients: the listening socket and where the question came from */
	int listen_fd;
	struct sockaddr_storage peer;
	socklen_t peer_len;
	gboolean has_pktinfo;
	guint8 pktinfo[64];
	gsize pktinfo_len;
	int pktinfo_level, pktinfo_type;
	TcpClient *tcp;
	char *client_name;
	char *app; /* which app of the phone asked (appid.h), NULL if unknown or not the phone */
	char *asker; /* key in self.requests_per while counted there (REQUESTS_PER_ASKER) */
	DnsQuery q;
	gint64 start;
	gint64 wall;
	Decision d;
	char *via; /* the CNAME that was blocked */
	/* Servers to ask, in order */
	GPtrArray *ups;
	guint next;
	Upstream *cur;
	guint16 up_id;
	guint attempt;
	int usock;
	guint usource;
	guint timer;
	guint deadline;
	GCancellable *cancel;
	gboolean tcp_retry;
	gboolean in_stream;
	/* Internal lookups */
	InternalDone done;
	gpointer done_data;
	gboolean no_cache;
};

struct Stream {
	int ref;
	Upstream *up; /* NULL once the server is gone */
	gboolean tls;
	gboolean closing;
	guint generation; /* counts connections; callbacks of an old one are ignored */
	char *spec;
	char *host;
	guint16 port;
	GSocketAddress *addr;
	GIOStream *io;
	gboolean connecting;
	GQueue waiting;		/* Request* waiting for the connection */
	GHashTable *inflight;	/* id -> Request* */
	GQueue writes;		/* GBytes* */
	gboolean writing;
	guint8 len_buf[2];
	guint8 *body;
	gsize body_len;
	GCancellable *cancel;
	guint idle;
};

struct TcpClient {
	int ref;
	GSocketConnection *conn;
	ClientKind kind;
	char *peer; /* address of a hotspot/USB client (counted per device), NULL for the phone */
	gboolean reading; /* a read of length or body is under way */
	gsize queued;	  /* bytes in writes */
	char *client_name;
	char *app;
	guint8 len_buf[2];
	guint8 *body;
	gsize body_len;
	GQueue writes;
	gboolean writing;
	gboolean closed;
	guint pending;
	guint idle;
	guint lifetime;
	GList *link; /* in self.tcp_phone_q or self.tcp_shared_q, oldest first */
	GCancellable *cancel;
};

/* ---- Cache ---- */

typedef struct {
	char *key;
	GBytes *msg;
	gint64 stored; /* monotonic seconds */
	guint32 ttl;
	GList *link;
} CacheEntry;

/* ---- Statistics and log ---- */

typedef struct {
	gint64 hour; /* unix seconds / 3600 */
	guint32 total;
	guint32 blocked;
	guint32 cached;
	guint32 failed;
	guint64 sum_us;
	/* name -> count, NULL while nothing was blocked: Most Blocked covers the same hours as the count */
	GHashTable *blocked_names;
} HourStat;

typedef struct {
	gint64 day; /* unix seconds / 86400 */
	GHashTable *domains;
	GHashTable *blocked; /* only in files of version 1 and 2; now per hour */
	GHashTable *clients;
	GHashTable *upstreams; /* spec -> guint64[2]: answers, sum of their times in µs */
	GHashTable *apps;
	GHashTable *pairs; /* "<who>\n<name>" -> count; who: app key, else "\t<kind>\t<client>" */
} DayTop;

typedef struct {
	guint32 id;
	gint64 time; /* unix microseconds */
	char *name;
	guint16 type;
	ClientKind kind;
	char *client;
	char *app;
	Status status;
	char *reason;
	char *via;
	char *answer;
	char *upstream;
	guint32 duration;
	guint16 rcode;
	gboolean cached;
	/* Bytes outside letters, digits, '-' and '_', or longer than a name can be: logged, but
	 * counted in the totals only, never as a name of its own */
	gboolean odd;
} LogEntry;

/* ---- Settings ---- */

typedef struct {
	gboolean enabled;
	char *block_mode; /* "null-ip", "nxdomain", "refused" */
	gboolean custom;
	char **servers;
	gboolean local_via_network;
	gboolean shared_clients;
	gboolean redirect_dns;
	gboolean block_encrypted;
	gboolean keep_log;
	guint log_size;
	guint stats_days;
	guint update_hours;
	gboolean update_on_mobile;
} Settings;

static struct {
	GMainLoop *loop;
	GDBusConnection *bus;
	GDBusConnection *system_bus;
	char *state_dir;
	char *link_dir;
	guint16 port;
	gboolean system; /* touch resolv.conf and iptables */
	gboolean ignore_portal;
	Settings s;
	gint64 paused_until;

	GPtrArray *lists;
	GPtrArray *rules; /* char* in canonical text; a rule for one app ends in "$app=<key>" */
	FilterSet *own;
	GHashTable *app_own; /* app key (appid.h) -> FilterSet* of its own rules */

	/* Network DNS from NetworkManager */
	GPtrArray *links; /* NetLink* sorted by priority */
	guint32 connectivity;
	guint32 metered;
	GPtrArray *custom; /* Upstream* */

	/* Sockets */
	GPtrArray *udp_fds; /* int as pointer: our listening sockets */
	GArray *udp_sources;
	GSocketService *tcp_service;
	guint tcp_clients;
	guint tcp_shared;
	GHashTable *tcp_peers; /* address -> connections (hotspot/USB) */
	GHashTable *requests_per; /* app, else client -> requests in flight */
	/* The open TCP connections of each kind, oldest first */
	GQueue tcp_phone_q;
	GQueue tcp_shared_q;
	gboolean listening;
	gboolean shared_listening;
	guint requests;

	GHashTable *cache; /* key -> CacheEntry* */
	GQueue cache_lru;

	GArray *hours; /* HourStat */
	GPtrArray *days; /* DayTop* */
	LogEntry *log;
	guint log_cap;
	guint log_head; /* next slot */
	guint log_count;
	guint32 log_next_id;
	GPtrArray *log_batch; /* GVariant* waiting for the live signal */
	guint log_batch_source;
	GHashTable *watchers; /* bus name -> watch id */

	SoupSession *soup;
	guint update_source;
	guint pause_source;
	guint idle_source;
	guint save_source;
	GFileMonitor *nm_monitor;
	gboolean stats_dirty;
	gboolean log_dirty;
	char *status;
	char *last_error;
} self;

static void emit_changed(const char *property, GVariant *value);
static void request_unref(Request *r);
static void request_next(Request *r);
static void request_finish_answer(Request *r, const guint8 *msg, gsize len, const char *upstream);
static void stream_forget(Stream *st, Request *r);
static void apply_state(void);
static void schedule_updates(guint delay_s);
static void tcp_client_unref(TcpClient *c);
static void tcp_client_reply(TcpClient *c, GBytes *msg);
static void tcp_request_finished(TcpClient *c);
static void tcp_client_close(TcpClient *c);
static void update_status(void);
static void touch_idle(void);

/* ===================================================================== helpers */

static gboolean run(const char *const *argv, gboolean quiet)
{
	g_autoptr(GError) error = NULL;
	g_autofree char *err = NULL;
	int status;

	if (!g_spawn_sync(NULL, (char **)argv, NULL, G_SPAWN_SEARCH_PATH | G_SPAWN_STDOUT_TO_DEV_NULL, NULL, NULL, NULL,
			  quiet ? &err : NULL, &status, &error)) {
		g_warning("Cannot run %s: %s", argv[0], error->message);
		return FALSE;
	}
	return g_spawn_check_wait_status(status, NULL);
}

static char *state_path(const char *name)
{
	return g_build_filename(self.state_dir, name, NULL);
}

/* Settings, rules, statistics and the query log are private: 0600 whatever the umask */
static gboolean save_private(const char *path, const void *data, gsize len, GError **error)
{
	return g_file_set_contents_full(path, data, len, G_FILE_SET_CONTENTS_CONSISTENT, 0600, error);
}

static gboolean save_key_file(GKeyFile *kf, const char *path, GError **error)
{
	gsize len;
	g_autofree char *data = g_key_file_to_data(kf, &len, NULL);

	return save_private(path, data, len, error);
}

/* Files of earlier versions were world-readable (0644 in a 0755 directory); now owner only */
static void fix_state_modes(const char *dir)
{
	g_autoptr(GDir) d = g_dir_open(dir, 0, NULL);
	const char *name;

	if (chmod(dir, 0700) < 0 && errno != ENOENT)
		g_warning("Cannot make %s private: %s", dir, g_strerror(errno));
	while (d && (name = g_dir_read_name(d))) {
		g_autofree char *path = g_build_filename(dir, name, NULL);
		struct stat st;

		if (lstat(path, &st) < 0)
			continue;
		if (S_ISDIR(st.st_mode))
			fix_state_modes(path);
		else if (S_ISREG(st.st_mode) && (st.st_mode & 077))
			chmod(path, 0600);
	}
}

static gint64 now_s(void)
{
	return g_get_real_time() / G_USEC_PER_SEC;
}

static gint64 mono_s(void)
{
	return g_get_monotonic_time() / G_USEC_PER_SEC;
}

static gboolean paused(void)
{
	return self.paused_until > now_s();
}

/* ===================================================================== settings */

static void load_settings(void)
{
	g_autoptr(GKeyFile) kf = g_key_file_new();
	g_autofree char *path = state_path("settings");
	g_autoptr(GError) error = NULL;
	Settings *s = &self.s;

	/* Defaults without a settings file: off; when on, everything of the phone goes through it */
	s->enabled = FALSE;
	s->block_mode = g_strdup("null-ip");
	s->custom = FALSE;
	s->servers = g_new0(char *, 1);
	s->local_via_network = TRUE;
	s->shared_clients = TRUE;
	s->redirect_dns = TRUE;
	s->block_encrypted = TRUE;
	s->keep_log = FALSE;
	s->log_size = 5000;
	s->stats_days = 7;
	s->update_hours = 24;
	s->update_on_mobile = FALSE;

	if (!g_key_file_load_from_file(kf, path, G_KEY_FILE_NONE, &error)) {
		if (!g_error_matches(error, G_FILE_ERROR, G_FILE_ERROR_NOENT))
			g_warning("%s: %s", path, error->message);
		return;
	}
#define GET_BOOL(key, field)                                                                                      \
	do {                                                                                                      \
		g_autoptr(GError) e = NULL;                                                                       \
		gboolean v = g_key_file_get_boolean(kf, "filter", key, &e);                                     \
		if (!e)                                                                                           \
			s->field = v;                                                                             \
	} while (0)
#define GET_UINT(key, field, lo, hi)                                                                              \
	do {                                                                                                      \
		g_autoptr(GError) e = NULL;                                                                       \
		guint64 v = g_key_file_get_uint64(kf, "filter", key, &e);                                       \
		if (!e)                                                                                           \
			s->field = CLAMP(v, lo, hi);                                                              \
	} while (0)
	GET_BOOL("enabled", enabled);
	GET_BOOL("custom-servers", custom);
	GET_BOOL("local-via-network", local_via_network);
	GET_BOOL("shared-clients", shared_clients);
	GET_BOOL("redirect-dns", redirect_dns);
	GET_BOOL("block-encrypted-dns", block_encrypted);
	GET_BOOL("keep-log", keep_log);
	GET_UINT("log-size", log_size, 100, 50000);
	GET_UINT("stats-days", stats_days, 1, 90);
	GET_UINT("update-hours", update_hours, 0, 24 * 30);
	GET_BOOL("update-on-mobile", update_on_mobile);
	{
		g_autofree char *mode = g_key_file_get_string(kf, "filter", "block-mode", NULL);

		if (mode && (!strcmp(mode, "null-ip") || !strcmp(mode, "nxdomain") || !strcmp(mode, "refused"))) {
			g_free(s->block_mode);
			s->block_mode = g_steal_pointer(&mode);
		}
	}
	{
		char **servers = g_key_file_get_string_list(kf, "filter", "servers", NULL, NULL);

		if (servers) {
			g_strfreev(s->servers);
			s->servers = servers;
		}
	}
	self.paused_until = g_key_file_get_int64(kf, "filter", "paused-until", NULL);
#undef GET_BOOL
#undef GET_UINT
}

static void save_settings(void)
{
	g_autoptr(GKeyFile) kf = g_key_file_new();
	g_autofree char *path = state_path("settings");
	g_autoptr(GError) error = NULL;
	Settings *s = &self.s;

	g_key_file_set_comment(kf, NULL, NULL, " plinius-dnsfilter: changed through Settings (org.plinius.DnsFilter)", NULL);
	g_key_file_set_boolean(kf, "filter", "enabled", s->enabled);
	g_key_file_set_string(kf, "filter", "block-mode", s->block_mode);
	g_key_file_set_boolean(kf, "filter", "custom-servers", s->custom);
	g_key_file_set_string_list(kf, "filter", "servers", (const char *const *)s->servers, g_strv_length(s->servers));
	g_key_file_set_boolean(kf, "filter", "local-via-network", s->local_via_network);
	g_key_file_set_boolean(kf, "filter", "shared-clients", s->shared_clients);
	g_key_file_set_boolean(kf, "filter", "redirect-dns", s->redirect_dns);
	g_key_file_set_boolean(kf, "filter", "block-encrypted-dns", s->block_encrypted);
	g_key_file_set_boolean(kf, "filter", "keep-log", s->keep_log);
	g_key_file_set_uint64(kf, "filter", "log-size", s->log_size);
	g_key_file_set_uint64(kf, "filter", "stats-days", s->stats_days);
	g_key_file_set_uint64(kf, "filter", "update-hours", s->update_hours);
	g_key_file_set_boolean(kf, "filter", "update-on-mobile", s->update_on_mobile);
	g_key_file_set_int64(kf, "filter", "paused-until", self.paused_until);
	if (!save_key_file(kf, path, &error))
		g_warning("Cannot save %s: %s", path, error->message);
}

/* ===================================================================== lists and rules */

static const CatalogEntry *catalog_find(const char *id)
{
	for (guint i = 0; i < G_N_ELEMENTS(catalog); i++)
		if (!strcmp(catalog[i].id, id))
			return &catalog[i];
	return NULL;
}

static void list_free(List *l)
{
	g_free(l->id);
	g_free(l->name);
	g_free(l->url);
	g_free(l->etag);
	g_free(l->last_modified);
	g_free(l->error);
	filter_set_free(l->set);
	g_free(l);
}

static List *list_find(const char *id)
{
	for (guint i = 0; i < self.lists->len; i++) {
		List *l = g_ptr_array_index(self.lists, i);

		if (!strcmp(l->id, id))
			return l;
	}
	return NULL;
}

static List *list_find_url(const char *url)
{
	for (guint i = 0; i < self.lists->len; i++) {
		List *l = g_ptr_array_index(self.lists, i);

		if (!strcmp(l->url, url))
			return l;
	}
	return NULL;
}

static char *list_bin_path(const List *l)
{
	g_autofree char *name = g_strconcat(l->id, ".bin", NULL);

	return g_build_filename(self.state_dir, "compiled", name, NULL);
}

static void save_lists(void)
{
	g_autoptr(GKeyFile) kf = g_key_file_new();
	g_autofree char *path = state_path("lists");
	g_autoptr(GError) error = NULL;

	for (guint i = 0; i < self.lists->len; i++) {
		List *l = g_ptr_array_index(self.lists, i);

		g_key_file_set_string(kf, l->id, "name", l->name);
		g_key_file_set_string(kf, l->id, "url", l->url);
		g_key_file_set_boolean(kf, l->id, "enabled", l->enabled);
		if (l->etag)
			g_key_file_set_string(kf, l->id, "etag", l->etag);
		if (l->last_modified)
			g_key_file_set_string(kf, l->id, "last-modified", l->last_modified);
		g_key_file_set_int64(kf, l->id, "updated", l->updated);
		g_key_file_set_int64(kf, l->id, "checked", l->checked);
		g_key_file_set_uint64(kf, l->id, "rules", l->rules);
		g_key_file_set_uint64(kf, l->id, "unsupported", l->unsupported);
		if (l->error)
			g_key_file_set_string(kf, l->id, "error", l->error);
	}
	if (!save_key_file(kf, path, &error))
		g_warning("Cannot save %s: %s", path, error->message);
}

static void list_load_set(List *l)
{
	g_autofree char *bin = list_bin_path(l);
	g_autoptr(GError) error = NULL;

	filter_set_free(l->set);
	l->set = NULL;
	if (!l->enabled || !g_file_test(bin, G_FILE_TEST_EXISTS))
		return;
	l->set = filter_set_load(bin, &error);
	if (!l->set)
		g_warning("List %s: %s", l->id, error->message);
}

static List *list_new(const char *id, const char *name, const char *url, gboolean enabled)
{
	List *l = g_new0(List, 1);

	l->id = g_strdup(id);
	l->name = g_strdup(name);
	l->url = g_strdup(url);
	l->enabled = enabled;
	return l;
}

static void load_lists(void)
{
	g_autoptr(GKeyFile) kf = g_key_file_new();
	g_autofree char *path = state_path("lists");
	g_autofree char *dir = g_build_filename(self.state_dir, "compiled", NULL);
	g_auto(GStrv) groups = NULL;

	self.lists = g_ptr_array_new_with_free_func((GDestroyNotify)list_free);
	g_mkdir_with_parents(dir, 0700);

	if (!g_key_file_load_from_file(kf, path, G_KEY_FILE_NONE, NULL)) {
		/* First start: HaGeZi Normal, switched on. Nothing is downloaded before the filter is. */
		const CatalogEntry *c = catalog_find(DEFAULT_LIST);

		g_ptr_array_add(self.lists, list_new(c->id, c->name, c->url, TRUE));
		save_lists();
		return;
	}
	groups = g_key_file_get_groups(kf, NULL);
	for (guint i = 0; groups[i]; i++) {
		g_autofree char *name = g_key_file_get_string(kf, groups[i], "name", NULL);
		g_autofree char *url = g_key_file_get_string(kf, groups[i], "url", NULL);
		List *l;

		if (!name || !url || strchr(groups[i], '/'))
			continue;
		l = list_new(groups[i], name, url, g_key_file_get_boolean(kf, groups[i], "enabled", NULL));
		l->etag = g_key_file_get_string(kf, groups[i], "etag", NULL);
		l->last_modified = g_key_file_get_string(kf, groups[i], "last-modified", NULL);
		l->updated = g_key_file_get_int64(kf, groups[i], "updated", NULL);
		l->checked = g_key_file_get_int64(kf, groups[i], "checked", NULL);
		l->rules = g_key_file_get_uint64(kf, groups[i], "rules", NULL);
		l->unsupported = g_key_file_get_uint64(kf, groups[i], "unsupported", NULL);
		l->error = g_key_file_get_string(kf, groups[i], "error", NULL);
		list_load_set(l);
		g_ptr_array_add(self.lists, l);
	}
}

/* "||example.com^$app=app/org.gnome.Epiphany": the app's key, and the rule without it in @base;
 * NULL for a rule of all apps */
static char *app_rule_split(const char *rule, char **base)
{
	const char *mark = strstr(rule, APP_MODIFIER);

	if (!mark || !mark[strlen(APP_MODIFIER)]) {
		*base = g_strdup(rule);
		return NULL;
	}
	*base = g_strndup(rule, mark - rule);
	return g_strdup(mark + strlen(APP_MODIFIER));
}

/* An app key as appid.h makes them: no spaces, no '$', not empty */
static gboolean valid_app_key(const char *key)
{
	gsize n = strlen(key);

	if (n == 0 || n > 255)
		return FALSE;
	for (const char *c = key; *c; c++)
		if (!g_ascii_isalnum(*c) && !strchr("._-/:@+~", *c))
			return FALSE;
	return TRUE;
}

static void rebuild_own(void)
{
	FilterBuilder *b = filter_builder_new();
	g_autoptr(GHashTable) builders = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
	GHashTableIter it;
	gpointer k, v;

	for (guint i = 0; i < self.rules->len; i++) {
		const char *rule = g_ptr_array_index(self.rules, i);
		g_autofree char *base = NULL;
		g_autofree char *app = app_rule_split(rule, &base);
		FilterBuilder *ab;

		if (!app) {
			filter_builder_add_text(b, rule, strlen(rule));
			continue;
		}
		ab = g_hash_table_lookup(builders, app);
		if (!ab) {
			ab = filter_builder_new();
			g_hash_table_insert(builders, g_strdup(app), ab);
		}
		filter_builder_add_text(ab, base, strlen(base));
	}
	filter_set_free(self.own);
	self.own = filter_builder_finish(b, NULL);
	if (!self.app_own)
		self.app_own = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, (GDestroyNotify)filter_set_free);
	g_hash_table_remove_all(self.app_own);
	g_hash_table_iter_init(&it, builders);
	while (g_hash_table_iter_next(&it, &k, &v))
		g_hash_table_insert(self.app_own, g_strdup(k), filter_builder_finish(v, NULL));
}

static void load_rules(void)
{
	g_autofree char *path = state_path("rules");
	g_autofree char *text = NULL;

	self.rules = g_ptr_array_new_with_free_func(g_free);
	if (g_file_get_contents(path, &text, NULL, NULL)) {
		g_auto(GStrv) lines = g_strsplit(text, "\n", -1);

		for (guint i = 0; lines[i]; i++) {
			g_strstrip(lines[i]);
			if (*lines[i] && lines[i][0] != '!')
				g_ptr_array_add(self.rules, g_strdup(lines[i]));
		}
	}
	rebuild_own();
}

static void save_rules(void)
{
	g_autofree char *path = state_path("rules");
	g_autoptr(GString) text = g_string_new("! Own rules of the DNS filter (Settings > Network & VPN > DNS Filter)\n");
	g_autoptr(GError) error = NULL;

	for (guint i = 0; i < self.rules->len; i++) {
		g_string_append(text, g_ptr_array_index(self.rules, i));
		g_string_append_c(text, '\n');
	}
	if (!save_private(path, text->str, text->len, &error))
		g_warning("Cannot save %s: %s", path, error->message);
}

typedef struct {
	char *domain;
	guint flags;
} FoundRule;

static void on_found_rule(const char *domain, guint flags, gboolean badfilter, gpointer data)
{
	FoundRule *f = data;

	if (!f->domain && !badfilter) {
		f->domain = g_strdup(domain);
		f->flags = flags;
	}
}

static char *canonical_rule_base(const char *input, GError **error);

/* Turns what the user typed into a rule: a bare domain blocks it with its subdomains; a rule may
 * end in "$app=<key>" and then holds for that app only */
static char *canonical_rule(const char *input, GError **error)
{
	g_autofree char *text = g_strstrip(g_strdup(input));
	g_autofree char *base = NULL;
	g_autofree char *app = app_rule_split(text, &base);
	g_autofree char *rule = NULL;

	if (!app)
		return canonical_rule_base(text, error);
	if (!valid_app_key(app) || strchr(base, '$')) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT, "Not a rule the filter understands: %s", input);
		return NULL;
	}
	rule = canonical_rule_base(base, error);
	return rule ? g_strconcat(rule, APP_MODIFIER, app, NULL) : NULL;
}

static char *canonical_rule_base(const char *input, GError **error)
{
	g_autofree char *text = g_strstrip(g_strdup(input));
	g_autofree char *ascii = NULL;
	FoundRule found = {0};
	gboolean bare;
	ParseResult res;

	bare = !strpbrk(text, "|@^$ \t*");
	if (bare) {
		/* International names as punycode; a pasted URL gives its host */
		g_autoptr(GUri) uri = strstr(text, "://") ? g_uri_parse(text, G_URI_FLAGS_NONE, NULL) : NULL;
		const char *host = uri && g_uri_get_host(uri) ? g_uri_get_host(uri) : text;

		ascii = g_hostname_to_ascii(host);
		if (!ascii) {
			g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT, "Not a domain: %s", input);
			return NULL;
		}
		g_autofree char *lower = g_ascii_strdown(ascii, -1);

		g_free(text);
		text = g_strdup_printf("||%s^", lower);
	}
	res = filter_parse_line(text, strlen(text), on_found_rule, &found);
	if (res != PARSE_RULE || !found.domain) {
		g_free(found.domain);
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT, "Not a rule the filter understands: %s", input);
		return NULL;
	}
	{
		char *rule = filter_rule_text(found.domain, found.flags);

		g_free(found.domain);
		return rule;
	}
}

static gboolean has_rule(const char *rule)
{
	for (guint i = 0; i < self.rules->len; i++)
		if (!strcmp(g_ptr_array_index(self.rules, i), rule))
			return TRUE;
	return FALSE;
}

/* ===================================================================== decisions */

/* Names the phone itself needs; no rule blocks them */
static gboolean system_allowed(const char *name)
{
	static const char *const names[] = {
		/* NetworkManager's connectivity check (Arch default and GNOME's) */
		"ping.archlinux.org",
		"nmcheck.gnome.org",
		/* Time */
		"pool.ntp.org",
		"time.cloudflare.com",
		/* Location (beaconDB, GPS assistance) */
		"beacondb.net",
		/* Our blocklists */
		"raw.githubusercontent.com",
		"small.oisd.nl",
		"big.oisd.nl",
		"adguardteam.github.io",
	};

	for (guint i = 0; i < G_N_ELEMENTS(names); i++) {
		gsize nl = strlen(name), l = strlen(names[i]);

		if (nl >= l && !strcmp(name + nl - l, names[i]) && (nl == l || name[nl - l - 1] == '.'))
			return TRUE;
	}
	return FALSE;
}

static void decision_clear(Decision *d)
{
	g_clear_pointer(&d->reason, g_free);
}

/* What the filter does with @name asked by @app (appid.h key, NULL: unknown or a device); the app's
 * own rules come first, so an app can be allowed what all others are denied and the other way round */
static Decision decide(const char *name, const char *app)
{
	FilterHit block, allow, important;
	FilterHit l_block = {0}, l_allow = {0}, l_important = {0};
	List *block_list = NULL, *allow_list = NULL, *important_list = NULL;
	Decision d = {STATUS_PASSED, NULL};

	if (!self.s.enabled || paused()) {
		d.status = STATUS_UNFILTERED;
		return d;
	}
	if (!strcmp(name, "use-application-dns.net")) {
		d.status = STATUS_BLOCKED_SPECIAL;
		return d;
	}
	if (system_allowed(name)) {
		d.status = STATUS_ALLOWED_SYSTEM;
		return d;
	}
	if (app && *app && self.app_own) {
		FilterSet *set = g_hash_table_lookup(self.app_own, app);

		if (set) {
			filter_set_lookup(set, name, &block, &allow, NULL);
			if (allow.found || block.found) {
				g_autofree char *base = filter_rule_text(allow.found ? allow.domain : block.domain,
									 allow.found ? allow.flags : block.flags);

				d.status = allow.found ? STATUS_ALLOWED_RULE : STATUS_BLOCKED_RULE;
				d.reason = g_strconcat(base, APP_MODIFIER, app, NULL);
				return d;
			}
		}
	}
	filter_set_lookup(self.own, name, &block, &allow, NULL);
	if (allow.found) {
		d.status = STATUS_ALLOWED_RULE;
		d.reason = filter_rule_text(allow.domain, allow.flags);
		return d;
	}
	if (block.found) {
		d.status = STATUS_BLOCKED_RULE;
		d.reason = filter_rule_text(block.domain, block.flags);
		return d;
	}
	for (guint i = 0; i < self.lists->len; i++) {
		List *l = g_ptr_array_index(self.lists, i);

		if (!l->enabled || !l->set)
			continue;
		filter_set_lookup(l->set, name, &block, &allow, &important);
		if (important.found && !l_important.found) {
			l_important = important;
			important_list = l;
		}
		if (allow.found && !l_allow.found) {
			l_allow = allow;
			allow_list = l;
		}
		if (block.found && !l_block.found) {
			l_block = block;
			block_list = l;
		}
	}
	if (l_important.found) {
		d.status = STATUS_BLOCKED_LIST;
		d.reason = g_strdup(important_list->name);
	} else if (l_allow.found) {
		d.status = STATUS_ALLOWED_LIST;
		d.reason = g_strdup(allow_list->name);
	} else if (l_block.found) {
		d.status = STATUS_BLOCKED_LIST;
		d.reason = g_strdup(block_list->name);
	}
	return d;
}

/* ===================================================================== upstreams */

static Upstream *upstream_ref(Upstream *u)
{
	u->ref++;
	return u;
}

static void stream_detach(Stream *st);

static void upstream_unref(Upstream *u)
{
	if (!u || --u->ref > 0)
		return;
	if (u->stream)
		stream_detach(g_steal_pointer(&u->stream));
	g_free(u->spec);
	g_clear_object(&u->addr);
	g_free(u->host);
	g_free(u->url);
	g_free(u->iface);
	g_free(u);
}

/* "9.9.9.9", "[2620:fe::fe]:53", "2620:fe::fe", "udp://…", "tls://dns.quad9.net[:853]",
 * "https://dns.quad9.net/dns-query" */
static Upstream *upstream_parse(const char *spec, GError **error)
{
	g_autofree char *s = g_strstrip(g_strdup(spec));
	Upstream *u = g_new0(Upstream, 1);

	u->ref = 1;
	u->spec = g_strdup(s);
	if (g_str_has_prefix(s, "https://")) {
		g_autoptr(GUri) uri = g_uri_parse(s, G_URI_FLAGS_NONE, error);

		/* GUri gives "" as the host of "https://" and "https:///dns-query" */
		if (!uri || !g_uri_get_host(uri) || !*g_uri_get_host(uri)) {
			upstream_unref(u);
			if (error && !*error)
				g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT, "No host in %s", spec);
			return NULL;
		}
		u->proto = PROTO_HTTPS;
		u->url = g_strdup(s);
		return u;
	}
	if (g_str_has_prefix(s, "tls://")) {
		g_autoptr(GNetworkAddress) na = NULL;

		na = (GNetworkAddress *)g_network_address_parse(s + 6, 853, error);
		if (na && !*g_network_address_get_hostname(na)) {
			/* "tls://" parses as a server with an empty name */
			g_clear_object(&na);
			g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT, "No host in %s", spec);
		}
		if (!na) {
			upstream_unref(u);
			return NULL;
		}
		u->proto = PROTO_TLS;
		u->host = g_strdup(g_network_address_get_hostname(na));
		u->port = g_network_address_get_port(na);
		return u;
	}
	{
		const char *addr = g_str_has_prefix(s, "udp://") ? s + 6 : s;
		g_autoptr(GInetAddress) ia = g_inet_address_new_from_string(addr);

		u->proto = PROTO_UDP;
		if (ia) {
			u->addr = g_inet_socket_address_new(ia, 53);
		} else {
			g_autoptr(GNetworkAddress) na = (GNetworkAddress *)g_network_address_parse(addr, 53, NULL);
			g_autoptr(GInetAddress) host = na ? g_inet_address_new_from_string(g_network_address_get_hostname(na)) : NULL;

			if (!host) {
				g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
					    "Plain DNS servers need an address (%s); use tls:// or https:// for a name", spec);
				upstream_unref(u);
				return NULL;
			}
			u->addr = g_inet_socket_address_new(host, g_network_address_get_port(na));
		}
	}
	return u;
}

static void netlink_free(NetLink *l)
{
	g_free(l->iface);
	g_strfreev(l->domains);
	g_ptr_array_unref(l->servers);
	g_free(l);
}

static int compare_links(gconstpointer a, gconstpointer b)
{
	const NetLink *x = *(NetLink *const *)a, *y = *(NetLink *const *)b;

	if (x->vpn != y->vpn)
		return x->vpn ? -1 : 1;
	return x->priority - y->priority;
}

static void cache_flush(void);

static void set_custom_servers(void)
{
	g_ptr_array_set_size(self.custom, 0);
	for (guint i = 0; self.s.servers[i]; i++) {
		g_autoptr(GError) error = NULL;
		Upstream *u = upstream_parse(self.s.servers[i], &error);

		if (u)
			g_ptr_array_add(self.custom, u);
		else
			g_warning("Server %s: %s", self.s.servers[i], error->message);
	}
	cache_flush();
}

/* Is @name inside @domain (or equal)? */
static gboolean in_domain(const char *name, const char *domain)
{
	gsize nl = strlen(name), dl = strlen(domain);

	if (!dl)
		return TRUE;
	return nl >= dl && !g_ascii_strcasecmp(name + nl - dl, domain) && (nl == dl || name[nl - dl - 1] == '.');
}

/* Names that only the local network knows: single labels, .local, reverse zones of private
 * and link-local addresses */
static gboolean local_name(const char *name)
{
	static const char *const zones[] = {
		"local",	     "10.in-addr.arpa",	  "168.192.in-addr.arpa", "254.169.in-addr.arpa",
		"d.f.ip6.arpa",	     "8.e.f.ip6.arpa",	  "9.e.f.ip6.arpa",	  "a.e.f.ip6.arpa",
		"b.e.f.ip6.arpa",    "home.arpa",	  "lan",		  "internal",
	};

	if (!strchr(name, '.'))
		return TRUE;
	for (guint i = 0; i < G_N_ELEMENTS(zones); i++)
		if (in_domain(name, zones[i]))
			return TRUE;
	/* 172.16.0.0/12 */
	for (int i = 16; i < 32; i++) {
		char zone[32];

		g_snprintf(zone, sizeof(zone), "%d.172.in-addr.arpa", i);
		if (in_domain(name, zone))
			return TRUE;
	}
	return FALSE;
}

static void add_unique(GPtrArray *out, Upstream *u)
{
	for (guint i = 0; i < out->len; i++)
		if (g_ptr_array_index(out, i) == u)
			return;
	g_ptr_array_add(out, upstream_ref(u));
}

/* A domain of a network that may take names to that network's servers. A network hands out
 * search domains by DHCP; one that hands out "com" or "co.uk" would get every such name in
 * plain text, past the own encrypted servers. So: at least two labels and not a public suffix
 * of two labels, except the names only local networks use (lan, local, internal). */
static gboolean usable_domain(const char *d)
{
	static const char *const suffixes[] = {
		"co.uk",  "org.uk", "ac.uk",  "gov.uk", "me.uk",  "com.au", "net.au", "org.au", "co.nz",  "co.jp",
		"ne.jp",  "or.jp",  "com.br", "com.cn", "com.tw", "com.hk", "co.in",  "co.za",  "com.mx", "com.ar",
		"com.tr", "co.kr",  "co.il",  "com.sg", "com.my", "co.id",  "com.ua", "com.pl", "co.at",  "or.at",
	};
	const char *dot;

	if (!*d || !strcmp(d, "."))
		return FALSE;
	dot = strchr(d, '.');
	if (!dot || !dot[1])
		return !g_ascii_strcasecmp(d, "lan") || !g_ascii_strcasecmp(d, "local") ||
		       !g_ascii_strcasecmp(d, "internal") || !g_ascii_strcasecmp(d, "home") ||
		       !g_ascii_strcasecmp(d, "localdomain");
	if (!strchr(dot + 1, '.'))
		for (guint i = 0; i < G_N_ELEMENTS(suffixes); i++)
			if (!g_ascii_strcasecmp(d, suffixes[i]))
				return FALSE;
	return TRUE;
}

/* Network servers for @name: links whose routing domain matches it best, else all by priority */
static void network_upstreams(const char *name, GPtrArray *out)
{
	gsize best = 0;
	gboolean any = FALSE;

	for (guint i = 0; i < self.links->len; i++) {
		NetLink *l = g_ptr_array_index(self.links, i);

		for (guint k = 0; l->domains && l->domains[k]; k++) {
			const char *d = l->domains[k][0] == '~' ? l->domains[k] + 1 : l->domains[k];

			if (usable_domain(d) && in_domain(name, d) && strlen(d) >= best) {
				best = strlen(d);
				any = TRUE;
			}
		}
	}
	for (guint i = 0; any && i < self.links->len; i++) {
		NetLink *l = g_ptr_array_index(self.links, i);

		for (guint k = 0; l->domains && l->domains[k]; k++) {
			const char *d = l->domains[k][0] == '~' ? l->domains[k] + 1 : l->domains[k];

			if (usable_domain(d) && strlen(d) == best && in_domain(name, d))
				for (guint j = 0; j < l->servers->len; j++)
					add_unique(out, g_ptr_array_index(l->servers, j));
		}
	}
	if (any)
		return;
	for (guint i = 0; i < self.links->len; i++) {
		NetLink *l = g_ptr_array_index(self.links, i);

		for (guint j = 0; j < l->servers->len; j++)
			add_unique(out, g_ptr_array_index(l->servers, j));
	}
}

/* Is @name in a routing or search domain of a network (router.lan, a VPN's company.example)? */
static gboolean network_domain(const char *name)
{
	for (guint i = 0; i < self.links->len; i++) {
		NetLink *l = g_ptr_array_index(self.links, i);

		for (guint k = 0; l->domains && l->domains[k]; k++) {
			const char *d = l->domains[k][0] == '~' ? l->domains[k] + 1 : l->domains[k];

			if (usable_domain(d) && in_domain(name, d))
				return TRUE;
		}
	}
	return FALSE;
}

/* Servers that recently failed go last */
static int compare_health(gconstpointer a, gconstpointer b)
{
	const Upstream *x = *(Upstream *const *)a, *y = *(Upstream *const *)b;
	gboolean bx = x->failures >= 3 && mono_s() - x->failed_at < 60;
	gboolean by = y->failures >= 3 && mono_s() - y->failed_at < 60;

	return bx - by;
}

static GPtrArray *pick_upstreams(const char *name, gboolean internal)
{
	GPtrArray *out = g_ptr_array_new_with_free_func((GDestroyNotify)upstream_unref);
	/* At a login page (captive portal) only the network's DNS gets us to it. A network can fake
	 * this state (NetworkManager's check is plain HTTP) and then sees all names in plain text;
	 * that is the price of being able to log in at all (Android does the same unless "Private
	 * DNS" is strict). The names still go through our block rules. */
	gboolean portal = self.connectivity == 2 && !self.ignore_portal;

	if (internal || !self.s.custom || !self.custom->len || portal ||
	    (self.s.local_via_network && (local_name(name) || network_domain(name)))) {
		network_upstreams(name, out);
	} else {
		for (guint i = 0; i < self.custom->len; i++)
			add_unique(out, g_ptr_array_index(self.custom, i));
	}
	/* g_ptr_array_sort is stable since GLib 2.76? Not documented; ties keep order with this */
	if (out->len > 1)
		g_ptr_array_sort(out, compare_health);
	return out;
}

/* ===================================================================== cache */

static void cache_entry_free(CacheEntry *e)
{
	g_free(e->key);
	g_bytes_unref(e->msg);
	g_free(e);
}

static char *cache_key(const DnsQuery *q)
{
	return g_strdup_printf("%s|%u|%u|%c%c", q->name, q->type, q->klass, q->do_bit ? 'd' : '-',
			       (q->flags & DNS_FLAG_CD) ? 'c' : '-');
}

static void cache_flush(void)
{
	if (!self.cache)
		return;
	g_hash_table_remove_all(self.cache);
	g_queue_clear(&self.cache_lru);
}

static void cache_remove(CacheEntry *e)
{
	g_queue_delete_link(&self.cache_lru, e->link);
	g_hash_table_remove(self.cache, e->key);
}

static GBytes *cache_get(const DnsQuery *q, guint32 *age)
{
	g_autofree char *key = cache_key(q);
	CacheEntry *e = g_hash_table_lookup(self.cache, key);
	gint64 a;

	if (!e)
		return NULL;
	a = mono_s() - e->stored;
	if (a >= e->ttl) {
		cache_remove(e);
		return NULL;
	}
	g_queue_unlink(&self.cache_lru, e->link);
	g_queue_push_head_link(&self.cache_lru, e->link);
	*age = (guint32)a;
	return g_bytes_ref(e->msg);
}

static void cache_put(const DnsQuery *q, const guint8 *msg, gsize len, const DnsAnswer *a)
{
	CacheEntry *e;
	guint32 ttl;

	if (a->rcode != DNS_RCODE_NOERROR && a->rcode != DNS_RCODE_NXDOMAIN)
		return;
	if (a->flags & DNS_FLAG_TC)
		return;
	if (a->has_ttl)
		ttl = MIN(a->min_ttl, CACHE_TTL_MAX);
	else
		ttl = 0;
	if (a->rcode == DNS_RCODE_NXDOMAIN || a->answers == 0)
		ttl = MIN(ttl, CACHE_NEGATIVE_MAX);
	if (ttl == 0)
		return;
	e = g_new0(CacheEntry, 1);
	e->key = cache_key(q);
	e->msg = g_bytes_new(msg, len);
	e->stored = mono_s();
	e->ttl = ttl;
	{
		CacheEntry *old = g_hash_table_lookup(self.cache, e->key);

		if (old)
			cache_remove(old);
	}
	g_queue_push_head(&self.cache_lru, e);
	e->link = self.cache_lru.head;
	g_hash_table_insert(self.cache, e->key, e);
	while (self.cache_lru.length > CACHE_MAX)
		cache_remove(g_queue_peek_tail(&self.cache_lru));
}

/* ===================================================================== statistics */

static void hour_stat_clear(gpointer p)
{
	HourStat *h = p;

	g_clear_pointer(&h->blocked_names, g_hash_table_unref);
}

static GHashTable *count_table_new(void)
{
	return g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
}

static void day_top_free(DayTop *d)
{
	g_hash_table_unref(d->domains);
	g_hash_table_unref(d->blocked);
	g_hash_table_unref(d->clients);
	g_hash_table_unref(d->apps);
	g_hash_table_unref(d->pairs);
	g_hash_table_unref(d->upstreams);
	g_free(d);
}

static DayTop *day_top_new(gint64 day)
{
	DayTop *d = g_new0(DayTop, 1);

	d->day = day;
	d->domains = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
	d->blocked = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
	d->clients = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
	d->apps = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
	d->pairs = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
	d->upstreams = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, g_free);
	return d;
}

typedef struct {
	const char *key;
	guint count;
} Count;

static int compare_counts(gconstpointer a, gconstpointer b)
{
	const Count *x = a, *y = b;

	return x->count < y->count ? 1 : x->count > y->count ? -1 : strcmp(x->key, y->key);
}

/* Keeps the top @keep entries of a count table */
static void trim_table(GHashTable *t, guint keep)
{
	g_autoptr(GArray) all = g_array_new(FALSE, FALSE, sizeof(Count));
	g_autoptr(GPtrArray) drop = g_ptr_array_new_with_free_func(g_free);
	GHashTableIter it;
	gpointer k, v;

	g_hash_table_iter_init(&it, t);
	while (g_hash_table_iter_next(&it, &k, &v)) {
		Count c = {k, GPOINTER_TO_UINT(v)};

		g_array_append_val(all, c);
	}
	g_array_sort(all, compare_counts);
	for (guint i = keep; i < all->len; i++)
		g_ptr_array_add(drop, g_strdup(g_array_index(all, Count, i).key));
	for (guint i = 0; i < drop->len; i++)
		g_hash_table_remove(t, g_ptr_array_index(drop, i));
}

/* A day's table only keeps its top @keep entries; counted in a pointer (no allocation per name) */
static void count_in_keep(GHashTable *t, const char *key, guint keep)
{
	gpointer old;

	if (g_hash_table_lookup_extended(t, key, NULL, &old)) {
		g_hash_table_insert(t, g_strdup(key), GUINT_TO_POINTER(GPOINTER_TO_UINT(old) + 1));
		return;
	}
	/* Keep the top half; names below it come back if they are asked often */
	if (g_hash_table_size(t) >= keep * 2)
		trim_table(t, keep);
	g_hash_table_insert(t, g_strdup(key), GUINT_TO_POINTER(1));
}

static void count_in(GHashTable *t, const char *key)
{
	count_in_keep(t, key, TOP_KEEP);
}

/* Who asked, as the pairs and the name's page in Settings key it: the app, else the device */
static char *who_key(ClientKind kind, const char *client, const char *app)
{
	if (app && *app)
		return g_strdup(app);
	return g_strdup_printf("\t%s\t%s", client_kinds[kind], client ? client : "");
}

static HourStat *current_hour(void)
{
	gint64 hour = now_s() / 3600;
	HourStat *h;

	if (self.hours->len) {
		h = &g_array_index(self.hours, HourStat, self.hours->len - 1);
		if (h->hour == hour)
			return h;
	}
	{
		HourStat n = {.hour = hour};
		guint keep = self.s.stats_days * 24;

		g_array_append_val(self.hours, n);
		if (self.hours->len > keep)
			g_array_remove_range(self.hours, 0, self.hours->len - keep);
	}
	return &g_array_index(self.hours, HourStat, self.hours->len - 1);
}

static DayTop *current_day(void)
{
	gint64 day = now_s() / 86400;
	DayTop *d = self.days->len ? g_ptr_array_index(self.days, self.days->len - 1) : NULL;

	if (d && d->day == day)
		return d;
	d = day_top_new(day);
	g_ptr_array_add(self.days, d);
	while (self.days->len > self.s.stats_days)
		g_ptr_array_remove_index(self.days, 0);
	return d;
}

static void count_upstream(DayTop *d, const char *spec, guint32 duration_us)
{
	guint64 *v = g_hash_table_lookup(d->upstreams, spec);

	if (!v) {
		if (g_hash_table_size(d->upstreams) >= 32)
			return;
		v = g_new0(guint64, 2);
		g_hash_table_insert(d->upstreams, g_strdup(spec), v);
	}
	v[0]++;
	v[1] += duration_us;
}

static void stats_count(const LogEntry *e)
{
	HourStat *h = current_hour();
	DayTop *d = current_day();

	h->total++;
	h->sum_us += e->duration;
	if (status_blocked(e->status)) {
		h->blocked++;
		if (!e->odd) {
			if (!h->blocked_names)
				h->blocked_names = count_table_new();
			count_in_keep(h->blocked_names, e->name, HOUR_BLOCKED_KEEP);
		}
	}
	if (e->cached)
		h->cached++;
	if (e->status == STATUS_FAILED)
		h->failed++;
	self.stats_dirty = TRUE;
	if (e->odd)
		goto upstream;
	count_in(d->domains, e->name);
	{
		g_autofree char *client = g_strdup_printf("%s\t%s", client_kinds[e->kind], e->client ? e->client : "");

		count_in(d->clients, client);
	}
	if (e->app && *e->app)
		count_in(d->apps, e->app);
	{
		g_autofree char *who = who_key(e->kind, e->client, e->app);
		g_autofree char *pair = g_strconcat(who, "\n", e->name, NULL);

		count_in_keep(d->pairs, pair, PAIRS_KEEP);
	}
upstream:
	if (e->upstream && !e->cached)
		count_upstream(d, e->upstream, e->duration);
}

/* About what a table takes in the stats file: each key with its count and offset */
static gsize table_bytes(GHashTable *t)
{
	GHashTableIter it;
	gpointer k;
	gsize n = 0;

	if (!t)
		return 0;
	g_hash_table_iter_init(&it, t);
	while (g_hash_table_iter_next(&it, &k, NULL))
		n += strlen(k) + 16;
	return n;
}

static gsize stats_bytes(void)
{
	gsize n = 0;

	for (guint i = 0; i < self.hours->len; i++)
		n += 48 + table_bytes(g_array_index(self.hours, HourStat, i).blocked_names);
	for (guint i = 0; i < self.days->len; i++) {
		DayTop *d = g_ptr_array_index(self.days, i);

		n += 64 + table_bytes(d->domains) + table_bytes(d->blocked) + table_bytes(d->clients) +
		     table_bytes(d->apps) + table_bytes(d->pairs);
	}
	return n;
}

/* Keeps the statistics below STATS_MAX_BYTES: the oldest day first loses its names, with them the
 * blocked names of its hours. The counts (queries, blocked, per hour) and the servers stay */
static void stats_trim(void)
{
	gsize bytes = stats_bytes();

	for (guint i = 0; bytes > STATS_MAX_BYTES && i + 1 < self.days->len; i++) {
		DayTop *d = g_ptr_array_index(self.days, i);

		g_hash_table_remove_all(d->domains);
		g_hash_table_remove_all(d->blocked);
		g_hash_table_remove_all(d->clients);
		g_hash_table_remove_all(d->apps);
		g_hash_table_remove_all(d->pairs);
		for (guint j = 0; j < self.hours->len; j++) {
			HourStat *h = &g_array_index(self.hours, HourStat, j);

			if (h->hour * 3600 / 86400 <= d->day)
				g_clear_pointer(&h->blocked_names, g_hash_table_unref);
		}
		bytes = stats_bytes();
		g_message("Statistics over %u bytes: names of day %" G_GINT64_FORMAT " dropped", STATS_MAX_BYTES,
			  d->day);
		self.stats_dirty = TRUE;
	}
	/* Only today is left and still too large (not reached with the limits per table): its hours */
	for (guint j = 0; bytes > STATS_MAX_BYTES && j < self.hours->len; j++) {
		g_clear_pointer(&g_array_index(self.hours, HourStat, j).blocked_names, g_hash_table_unref);
		bytes = stats_bytes();
	}
}

static GVariant *top_variant(GHashTable *t, guint limit)
{
	g_autoptr(GArray) all = g_array_new(FALSE, FALSE, sizeof(Count));
	GVariantBuilder b;
	GHashTableIter it;
	gpointer k, v;

	g_hash_table_iter_init(&it, t);
	while (g_hash_table_iter_next(&it, &k, &v)) {
		Count c = {k, GPOINTER_TO_UINT(v)};

		g_array_append_val(all, c);
	}
	g_array_sort(all, compare_counts);
	g_variant_builder_init(&b, G_VARIANT_TYPE("a(su)"));
	for (guint i = 0; i < all->len && i < limit; i++)
		g_variant_builder_add(&b, "(su)", g_array_index(all, Count, i).key, g_array_index(all, Count, i).count);
	return g_variant_builder_end(&b);
}

static void merge_counts(GHashTable *into, GHashTable *from)
{
	GHashTableIter it;
	gpointer k, v;

	g_hash_table_iter_init(&it, from);
	while (g_hash_table_iter_next(&it, &k, &v)) {
		guint old = GPOINTER_TO_UINT(g_hash_table_lookup(into, k));

		g_hash_table_insert(into, g_strdup(k), GUINT_TO_POINTER(old + GPOINTER_TO_UINT(v)));
	}
}

/* Rules of the switched-on lists */
static guint active_rules(void)
{
	guint n = 0;

	for (guint i = 0; i < self.lists->len; i++) {
		List *l = g_ptr_array_index(self.lists, i);

		if (l->enabled && l->set)
			n += filter_set_size(l->set);
	}
	return n;
}

/* Statistics of the last @days days (1 = the last 24 hours) */
static GVariant *get_stats(guint days)
{
	GVariantBuilder b, series, ups;
	gint64 from_hour = now_s() / 3600 - (gint64)days * 24 + 1;
	gint64 from_day = now_s() / 86400 - days + 1;
	guint64 total = 0, blocked = 0, cached = 0, failed = 0, sum = 0;
	g_autoptr(GHashTable) domains = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
	g_autoptr(GHashTable) blocked_t = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
	g_autoptr(GHashTable) clients = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
	g_autoptr(GHashTable) apps = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
	g_autoptr(GHashTable) upstreams = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, g_free);
	GHashTableIter it;
	gpointer k, v;

	g_variant_builder_init(&series, G_VARIANT_TYPE("a(xuuuu)"));
	for (guint i = 0; i < self.hours->len; i++) {
		HourStat *h = &g_array_index(self.hours, HourStat, i);

		if (h->hour < from_hour)
			continue;
		total += h->total;
		blocked += h->blocked;
		cached += h->cached;
		failed += h->failed;
		sum += h->sum_us;
		if (h->blocked_names)
			merge_counts(blocked_t, h->blocked_names);
		g_variant_builder_add(&series, "(xuuuu)", h->hour * 3600, h->total, h->blocked, h->cached,
				      h->total ? (guint32)(h->sum_us / h->total) : 0);
	}
	for (guint i = 0; i < self.days->len; i++) {
		DayTop *d = g_ptr_array_index(self.days, i);

		if (d->day < from_day)
			continue;
		merge_counts(domains, d->domains);
		merge_counts(clients, d->clients);
		merge_counts(apps, d->apps);
		g_hash_table_iter_init(&it, d->upstreams);
		while (g_hash_table_iter_next(&it, &k, &v)) {
			guint64 *src = v, *dst = g_hash_table_lookup(upstreams, k);

			if (!dst) {
				dst = g_new0(guint64, 2);
				g_hash_table_insert(upstreams, g_strdup(k), dst);
			}
			dst[0] += src[0];
			dst[1] += src[1];
		}
	}
	g_variant_builder_init(&ups, G_VARIANT_TYPE("a(suu)"));
	g_hash_table_iter_init(&it, upstreams);
	while (g_hash_table_iter_next(&it, &k, &v)) {
		guint64 *c = v;

		g_variant_builder_add(&ups, "(suu)", (const char *)k, (guint32)c[0], c[0] ? (guint32)(c[1] / c[0]) : 0);
	}

	g_variant_builder_init(&b, G_VARIANT_TYPE_VARDICT);
	g_variant_builder_add(&b, "{sv}", "days", g_variant_new_uint32(days));
	g_variant_builder_add(&b, "{sv}", "queries", g_variant_new_uint64(total));
	g_variant_builder_add(&b, "{sv}", "blocked", g_variant_new_uint64(blocked));
	g_variant_builder_add(&b, "{sv}", "cached", g_variant_new_uint64(cached));
	g_variant_builder_add(&b, "{sv}", "failed", g_variant_new_uint64(failed));
	g_variant_builder_add(&b, "{sv}", "average-us", g_variant_new_uint32(total ? (guint32)(sum / total) : 0));
	g_variant_builder_add(&b, "{sv}", "series", g_variant_builder_end(&series));
	g_variant_builder_add(&b, "{sv}", "top-domains", top_variant(domains, 20));
	g_variant_builder_add(&b, "{sv}", "top-blocked", top_variant(blocked_t, 20));
	g_variant_builder_add(&b, "{sv}", "top-clients", top_variant(clients, 20));
	g_variant_builder_add(&b, "{sv}", "top-apps", top_variant(apps, 30));
	g_variant_builder_add(&b, "{sv}", "upstreams", g_variant_builder_end(&ups));
	g_variant_builder_add(&b, "{sv}", "rules", g_variant_new_uint32(active_rules()));
	return g_variant_builder_end(&b);
}

/* The phone's apps belong to the phone as a device: "\tphone\t…" covers them too */
static gboolean who_covers(const char *wanted, const char *who)
{
	if (!strcmp(wanted, who))
		return TRUE;
	return g_str_has_prefix(wanted, "\tphone\t") && who[0] != '\t';
}

/* The names @who asked in the last @days days (GetNamesOf), or who asked @name (GetAskersOf), most
 * first */
static GVariant *get_pairs(guint days, const char *who, const char *name, guint limit)
{
	gint64 from_day = now_s() / 86400 - days + 1;
	g_autoptr(GHashTable) counts = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);

	for (guint i = 0; i < self.days->len; i++) {
		DayTop *d = g_ptr_array_index(self.days, i);
		GHashTableIter it;
		gpointer k, v;

		if (d->day < from_day)
			continue;
		g_hash_table_iter_init(&it, d->pairs);
		while (g_hash_table_iter_next(&it, &k, &v)) {
			const char *nl = strchr(k, '\n');
			g_autofree char *pair_who = NULL;
			const char *key;
			guint old;

			if (!nl)
				continue;
			pair_who = g_strndup(k, nl - (const char *)k);
			if (who) {
				if (!who_covers(who, pair_who))
					continue;
				key = nl + 1;
			} else {
				if (strcmp(nl + 1, name))
					continue;
				key = pair_who;
			}
			old = GPOINTER_TO_UINT(g_hash_table_lookup(counts, key));
			g_hash_table_insert(counts, g_strdup(key), GUINT_TO_POINTER(old + GPOINTER_TO_UINT(v)));
		}
	}
	return top_variant(counts, limit);
}

static GVariant *table_variant(GHashTable *t)
{
	GVariantBuilder b;
	GHashTableIter it;
	gpointer k, v;

	g_variant_builder_init(&b, G_VARIANT_TYPE("a{su}"));
	g_hash_table_iter_init(&it, t);
	while (g_hash_table_iter_next(&it, &k, &v))
		g_variant_builder_add(&b, "{su}", (const char *)k, GPOINTER_TO_UINT(v));
	return g_variant_builder_end(&b);
}

/* The statistics file: version 2 has a table of apps per day (version 1 had none), version 3 the
 * blocked names per hour instead of per day (the day's table stays, empty) */
#define STATS_DAY_V1 "(xa{su}a{su}a{su}a{s(tt)})"
#define STATS_DAY_V2 "(xa{su}a{su}a{su}a{s(tt)}a{su})"
#define STATS_V1 "(ua(xuuuut)a" STATS_DAY_V1 ")"
#define STATS_V2 "(ua(xuuuut)a" STATS_DAY_V2 ")"
#define STATS_V3 "(ua(xuuuuta{su})a" STATS_DAY_V2 ")"
/* Version 4: who asked which name, per day */
#define STATS_DAY_V4 "(xa{su}a{su}a{su}a{s(tt)}a{su}a{su})"
#define STATS_V4 "(ua(xuuuuta{su})a" STATS_DAY_V4 ")"

static void save_stats(void)
{
	g_autofree char *path = state_path("stats");
	g_autoptr(GVariant) all = NULL;
	g_autoptr(GError) error = NULL;
	GVariantBuilder hours, days;

	stats_trim();
	g_variant_builder_init(&hours, G_VARIANT_TYPE("a(xuuuuta{su})"));
	for (guint i = 0; i < self.hours->len; i++) {
		HourStat *h = &g_array_index(self.hours, HourStat, i);
		GVariantBuilder names;
		GHashTableIter it;
		gpointer k, v;

		g_variant_builder_init(&names, G_VARIANT_TYPE("a{su}"));
		if (h->blocked_names) {
			g_hash_table_iter_init(&it, h->blocked_names);
			while (g_hash_table_iter_next(&it, &k, &v))
				g_variant_builder_add(&names, "{su}", (const char *)k, GPOINTER_TO_UINT(v));
		}
		g_variant_builder_add(&hours, "(xuuuuta{su})", h->hour, h->total, h->blocked, h->cached, h->failed,
				      h->sum_us, &names);
	}
	g_variant_builder_init(&days, G_VARIANT_TYPE("a" STATS_DAY_V4));
	for (guint i = 0; i < self.days->len; i++) {
		DayTop *d = g_ptr_array_index(self.days, i);
		GVariantBuilder ups;
		GHashTableIter it;
		gpointer k, v;

		g_variant_builder_init(&ups, G_VARIANT_TYPE("a{s(tt)}"));
		g_hash_table_iter_init(&it, d->upstreams);
		while (g_hash_table_iter_next(&it, &k, &v))
			g_variant_builder_add(&ups, "{s(tt)}", (const char *)k, ((guint64 *)v)[0], ((guint64 *)v)[1]);
		g_variant_builder_add(&days, "(x@a{su}@a{su}@a{su}a{s(tt)}@a{su}@a{su})", d->day, table_variant(d->domains),
				      table_variant(d->blocked), table_variant(d->clients), &ups, table_variant(d->apps),
				      table_variant(d->pairs));
	}
	all = g_variant_ref_sink(g_variant_new(STATS_V4, 4, &hours, &days));
	if (!save_private(path, g_variant_get_data(all), g_variant_get_size(all), &error))
		g_warning("Cannot save %s: %s", path, error->message);
	self.stats_dirty = FALSE;
}

static void load_table(GHashTable *t, GVariant *v)
{
	GVariantIter it;
	const char *k;
	guint32 c;

	g_variant_iter_init(&it, v);
	while (g_variant_iter_next(&it, "{&su}", &k, &c))
		g_hash_table_insert(t, g_strdup(k), GUINT_TO_POINTER(c));
}

/* Files before version 3 counted blocked names per day: each day's names go to its last hour with
 * blocks (or its last hour at all), so they stay in the windows that count that day's blocks */
static void migrate_blocked_days(void)
{
	for (guint i = 0; i < self.days->len; i++) {
		DayTop *d = g_ptr_array_index(self.days, i);
		HourStat *into = NULL;

		if (!g_hash_table_size(d->blocked))
			continue;
		for (guint j = 0; j < self.hours->len; j++) {
			HourStat *h = &g_array_index(self.hours, HourStat, j);

			if (h->hour * 3600 / 86400 != d->day)
				continue;
			if (!into || into->blocked == 0 || h->blocked > 0)
				into = h;
		}
		if (into) {
			if (!into->blocked_names)
				into->blocked_names = count_table_new();
			merge_counts(into->blocked_names, d->blocked);
		}
		g_hash_table_remove_all(d->blocked);
	}
}

static void load_stats(void)
{
	g_autofree char *path = state_path("stats");
	g_autofree char *data = NULL;
	gsize len;
	g_autoptr(GVariant) all = NULL;
	g_autoptr(GVariant) hours = NULL;
	g_autoptr(GVariant) days = NULL;
	guint32 version;
	GVariantIter it;

	self.hours = g_array_new(FALSE, FALSE, sizeof(HourStat));
	g_array_set_clear_func(self.hours, hour_stat_clear);
	self.days = g_ptr_array_new_with_free_func((GDestroyNotify)day_top_free);
	{
		GStatBuf st;

		if (g_stat(path, &st) < 0)
			return;
		if (st.st_size > STATS_FILE_MAX) {
			g_warning("%s has %" G_GINT64_FORMAT " bytes, more than %u: starting with empty statistics", path,
				  (gint64)st.st_size, STATS_FILE_MAX);
			return;
		}
	}
	if (!g_file_get_contents(path, &data, &len, NULL) || len > STATS_FILE_MAX)
		return;
	/* The version is the first field, at the start of the data in our byte order */
	if (len < 4)
		return;
	memcpy(&version, data, 4);
	if (version < 1 || version > 4)
		return;
	all = g_variant_new_from_data(G_VARIANT_TYPE(version == 1   ? STATS_V1
						     : version == 2 ? STATS_V2
						     : version == 3 ? STATS_V3
								    : STATS_V4),
				      data, len, FALSE, NULL, NULL);
	g_variant_ref_sink(all);
	/* Normal form: a damaged file reads as empty values instead of crashing us */
	{
		GVariant *n = g_variant_get_normal_form(all);

		g_variant_unref(all);
		all = n;
	}
	hours = g_variant_get_child_value(all, 1);
	days = g_variant_get_child_value(all, 2);
	{
		GVariant *row;

		g_variant_iter_init(&it, hours);
		while ((row = g_variant_iter_next_value(&it))) {
			HourStat h = {0};

			g_variant_get(row, version < 3 ? "(xuuuut)" : "(xuuuut*)", &h.hour, &h.total, &h.blocked, &h.cached,
				      &h.failed, &h.sum_us, NULL);
			if (version >= 3) {
				g_autoptr(GVariant) names = g_variant_get_child_value(row, 6);

				if (g_variant_n_children(names)) {
					h.blocked_names = count_table_new();
					load_table(h.blocked_names, names);
				}
			}
			g_array_append_val(self.hours, h);
			g_variant_unref(row);
		}
	}
	{
		gint64 day;
		GVariant *dom, *blk, *cli, *ups, *row;

		g_variant_iter_init(&it, days);
		while ((row = g_variant_iter_next_value(&it))) {
			DayTop *d;

			g_variant_get_child(row, 0, "x", &day);
			dom = g_variant_get_child_value(row, 1);
			blk = g_variant_get_child_value(row, 2);
			cli = g_variant_get_child_value(row, 3);
			ups = g_variant_get_child_value(row, 4);
			d = day_top_new(day);
			/* Version 3 has them too (0.1-7 read them from version 2 only) */
			if (version >= 2) {
				g_autoptr(GVariant) apps = g_variant_get_child_value(row, 5);

				load_table(d->apps, apps);
			}
			if (version >= 4) {
				g_autoptr(GVariant) pairs = g_variant_get_child_value(row, 6);

				load_table(d->pairs, pairs);
			}
			g_variant_unref(row);
			GVariantIter ui;
			const char *k;
			guint64 c, s;

			load_table(d->domains, dom);
			load_table(d->blocked, blk);
			load_table(d->clients, cli);
			g_variant_iter_init(&ui, ups);
			while (g_variant_iter_next(&ui, "{&s(tt)}", &k, &c, &s)) {
				guint64 *v = g_new(guint64, 2);

				v[0] = c;
				v[1] = s;
				g_hash_table_insert(d->upstreams, g_strdup(k), v);
			}
			g_ptr_array_add(self.days, d);
			g_variant_unref(dom);
			g_variant_unref(blk);
			g_variant_unref(cli);
			g_variant_unref(ups);
		}
	}
	if (version < 3)
		migrate_blocked_days();
	/* Files of 0.1-8 and before: no limit per hour and in all */
	for (guint i = 0; i < self.hours->len; i++) {
		HourStat *h = &g_array_index(self.hours, HourStat, i);

		if (h->blocked_names && g_hash_table_size(h->blocked_names) > HOUR_BLOCKED_KEEP * 2)
			trim_table(h->blocked_names, HOUR_BLOCKED_KEEP);
	}
	stats_trim();
}

/* ===================================================================== log */

static void log_entry_clear(LogEntry *e)
{
	g_free(e->name);
	g_free(e->client);
	g_free(e->app);
	g_free(e->reason);
	g_free(e->via);
	g_free(e->answer);
	g_free(e->upstream);
	memset(e, 0, sizeof(*e));
}

/* The last field is the app (appid.h), "" if unknown or not the phone */
#define LOG_TYPE "(uxsqssussssuqbs)"

static GVariant *log_variant(const LogEntry *e)
{
	return g_variant_new(LOG_TYPE, e->id, e->time, e->name, e->type, client_kinds[e->kind], e->client ? e->client : "",
			     (guint32)e->status, e->reason ? e->reason : "", e->via ? e->via : "", e->answer ? e->answer : "",
			     e->upstream ? e->upstream : "", e->duration, e->rcode, e->cached, e->app ? e->app : "");
}

static void log_resize(guint cap)
{
	LogEntry *n = g_new0(LogEntry, cap);
	guint keep = MIN(self.log_count, cap);

	/* Keep the newest */
	for (guint i = 0; i < self.log_count; i++) {
		guint src = (self.log_head + self.log_cap - self.log_count + i) % self.log_cap;

		if (i < self.log_count - keep)
			log_entry_clear(&self.log[src]);
		else
			n[i - (self.log_count - keep)] = self.log[src];
	}
	g_free(self.log);
	self.log = n;
	self.log_cap = cap;
	self.log_count = keep;
	self.log_head = keep % cap;
}

/* A signal only for the callers of Watch (the settings), each by name: the system bus hands an
 * undirected signal to anyone who asks for it, and the query log is private */
static void emit_to_watchers(const char *iface, const char *signal, GVariant *params)
{
	GHashTableIter it;
	gpointer name;

	g_variant_ref_sink(params);
	if (self.bus) {
		g_hash_table_iter_init(&it, self.watchers);
		while (g_hash_table_iter_next(&it, &name, NULL))
			g_dbus_connection_emit_signal(self.bus, name, OBJECT_PATH, iface, signal, params, NULL);
	}
	g_variant_unref(params);
}

static gboolean on_log_batch(gpointer user_data)
{
	GVariantBuilder b;

	self.log_batch_source = 0;
	if (!self.log_batch->len)
		return G_SOURCE_REMOVE;
	g_variant_builder_init(&b, G_VARIANT_TYPE("a" LOG_TYPE));
	for (guint i = 0; i < self.log_batch->len; i++)
		g_variant_builder_add_value(&b, g_ptr_array_index(self.log_batch, i));
	g_ptr_array_set_size(self.log_batch, 0);
	emit_to_watchers(INTERFACE, "Logged", g_variant_new("(a" LOG_TYPE ")", &b));
	return G_SOURCE_REMOVE;
}

static void log_add(LogEntry *e)
{
	LogEntry *slot = &self.log[self.log_head];

	e->id = ++self.log_next_id;
	log_entry_clear(slot);
	*slot = *e;
	self.log_head = (self.log_head + 1) % self.log_cap;
	if (self.log_count < self.log_cap)
		self.log_count++;
	stats_count(slot);
	self.log_dirty = TRUE;
	if (g_hash_table_size(self.watchers)) {
		g_ptr_array_add(self.log_batch, g_variant_ref_sink(log_variant(slot)));
		if (!self.log_batch_source)
			self.log_batch_source = g_timeout_add(LOG_BATCH_MS, on_log_batch, NULL);
	}
}

/* @filter: 0 all, 1 blocked, 2 allowed; with LOG_EXACT, @needle is the whole name */
#define LOG_EXACT 4

static gboolean log_matches(const LogEntry *e, const char *needle, guint filter)
{
	if ((filter & 3) == 1 && !status_blocked(e->status))
		return FALSE;
	if ((filter & 3) == 2 && status_blocked(e->status))
		return FALSE;
	if (!needle)
		return TRUE;
	if (filter & LOG_EXACT)
		return !strcmp(e->name, needle);
	return strstr(e->name, needle) || (e->client && strstr(e->client, needle)) ||
	       (e->app && strcasestr(e->app, needle)) || (e->answer && strstr(e->answer, needle));
}

static LogEntry *log_nth_newest(guint i)
{
	return &self.log[(self.log_head + self.log_cap - 1 - i) % self.log_cap];
}

/* Newest first, older than @before (0 = from the newest) */
static GVariant *get_log(guint32 before, guint limit, const char *search, guint filter)
{
	GVariantBuilder b;
	guint n = 0;
	g_autofree char *needle = search && *search ? g_ascii_strdown(search, -1) : NULL;

	g_variant_builder_init(&b, G_VARIANT_TYPE("a" LOG_TYPE));
	for (guint i = 0; i < self.log_count && n < limit; i++) {
		LogEntry *e = log_nth_newest(i);

		if ((before && e->id >= before) || !log_matches(e, needle, filter))
			continue;
		g_variant_builder_add_value(&b, log_variant(e));
		n++;
	}
	return g_variant_builder_end(&b);
}

/* The log by name: each name once with its newest query, how many queries match and how many of
 * them were blocked, the name asked last first; @offset names are skipped */
static GVariant *get_log_names(guint offset, guint limit, const char *search, guint filter)
{
	GVariantBuilder b;
	g_autofree char *needle = search && *search ? g_ascii_strdown(search, -1) : NULL;
	g_autoptr(GHashTable) seen = g_hash_table_new(g_str_hash, g_str_equal);
	g_autoptr(GPtrArray) newest = g_ptr_array_new();
	g_autoptr(GArray) counts = g_array_new(FALSE, TRUE, sizeof(guint32) * 2);

	for (guint i = 0; i < self.log_count; i++) {
		LogEntry *e = log_nth_newest(i);
		gpointer at;
		guint32 *c;

		if (!log_matches(e, needle, filter & 3))
			continue;
		if (g_hash_table_lookup_extended(seen, e->name, NULL, &at)) {
			c = (guint32 *)counts->data + 2 * GPOINTER_TO_UINT(at);
		} else {
			g_hash_table_insert(seen, e->name, GUINT_TO_POINTER(newest->len));
			g_ptr_array_add(newest, e);
			g_array_set_size(counts, newest->len);
			c = (guint32 *)counts->data + 2 * (newest->len - 1);
		}
		c[0]++;
		if (status_blocked(e->status))
			c[1]++;
	}
	g_variant_builder_init(&b, G_VARIANT_TYPE("a(" LOG_TYPE "uu)"));
	for (guint i = offset; i < newest->len && i - offset < limit; i++) {
		const guint32 *c = (const guint32 *)counts->data + 2 * i;

		g_variant_builder_add(&b, "(@" LOG_TYPE "uu)", log_variant(g_ptr_array_index(newest, i)), c[0], c[1]);
	}
	return g_variant_builder_end(&b);
}

static void clear_log(void)
{
	for (guint i = 0; i < self.log_cap; i++)
		log_entry_clear(&self.log[i]);
	self.log_count = 0;
	self.log_head = 0;
	self.log_dirty = TRUE;
}

static void save_log(void)
{
	g_autofree char *path = state_path("querylog");
	GVariantBuilder b;
	g_autoptr(GVariant) v = NULL;

	self.log_dirty = FALSE;
	if (!self.s.keep_log) {
		g_unlink(path);
		return;
	}
	g_variant_builder_init(&b, G_VARIANT_TYPE("a" LOG_TYPE));
	for (guint i = 0; i < self.log_count; i++) {
		guint idx = (self.log_head + self.log_cap - self.log_count + i) % self.log_cap;

		g_variant_builder_add_value(&b, log_variant(&self.log[idx]));
	}
	v = g_variant_ref_sink(g_variant_builder_end(&b));
	save_private(path, g_variant_get_data(v), g_variant_get_size(v), NULL);
}

static void load_log(void)
{
	/* The log of 0.1 (without apps) is not read */
	{
		g_autofree char *old = state_path("log");

		g_unlink(old);
	}
	g_autofree char *path = state_path("querylog");
	g_autofree char *data = NULL;
	gsize len;
	g_autoptr(GVariant) v = NULL;
	GVariantIter it;
	GVariant *row;

	self.log_cap = self.s.log_size;
	self.log = g_new0(LogEntry, self.log_cap);
	if (!self.s.keep_log || !g_file_get_contents(path, &data, &len, NULL))
		return;
	v = g_variant_ref_sink(g_variant_new_from_data(G_VARIANT_TYPE("a" LOG_TYPE), data, len, FALSE, NULL, NULL));
	{
		GVariant *n = g_variant_get_normal_form(v);

		g_variant_unref(v);
		v = n;
	}
	g_variant_iter_init(&it, v);
	while ((row = g_variant_iter_next_value(&it))) {
		LogEntry e = {0};
		const char *kind;
		guint32 status;
		guint16 rcode;
		LogEntry *slot = &self.log[self.log_head];

		g_variant_get(row, LOG_TYPE, &e.id, &e.time, &e.name, &e.type, &kind, &e.client, &status, &e.reason, &e.via,
			      &e.answer, &e.upstream, &e.duration, &rcode, &e.cached, &e.app);
		e.status = status <= STATUS_BLOCKED_SPECIAL ? status : STATUS_PASSED;
		e.rcode = rcode;
		for (guint k = 0; k < G_N_ELEMENTS(client_kinds); k++)
			if (!strcmp(kind, client_kinds[k]))
				e.kind = k;
		g_free((char *)kind);
		log_entry_clear(slot);
		*slot = e;
		self.log_head = (self.log_head + 1) % self.log_cap;
		if (self.log_count < self.log_cap)
			self.log_count++;
		self.log_next_id = MAX(self.log_next_id, e.id);
		g_variant_unref(row);
	}
}

/* ===================================================================== clients */

/* Name of a hotspot client from NetworkManager's dnsmasq leases ("expiry mac ip name id") */
static char *hotspot_name(const char *ip)
{
	g_autoptr(GDir) dir = g_dir_open("/var/lib/NetworkManager", 0, NULL);
	const char *f;

	while (dir && (f = g_dir_read_name(dir))) {
		g_autofree char *path = NULL;
		g_autofree char *text = NULL;
		g_auto(GStrv) lines = NULL;

		if (!g_str_has_prefix(f, "dnsmasq-") || !g_str_has_suffix(f, ".leases"))
			continue;
		path = g_build_filename("/var/lib/NetworkManager", f, NULL);
		if (!g_file_get_contents(path, &text, NULL, NULL))
			continue;
		lines = g_strsplit(text, "\n", -1);
		for (guint i = 0; lines[i]; i++) {
			g_auto(GStrv) t = g_strsplit(lines[i], " ", 5);

			if (g_strv_length(t) >= 4 && !strcmp(t[2], ip) && strcmp(t[3], "*"))
				return g_strdup(t[3]);
		}
	}
	return NULL;
}

static ClientKind shared_kind(int ifindex)
{
	char name[IF_NAMESIZE] = "";

	if (!if_indextoname(ifindex, name))
		return CLIENT_INTERNAL;
	if (g_str_has_prefix(name, "ap"))
		return CLIENT_HOTSPOT;
	if (g_str_has_prefix(name, "usb"))
		return CLIENT_USB;
	return CLIENT_INTERNAL;
}

static char *peer_text(const struct sockaddr_storage *ss)
{
	char text[INET6_ADDRSTRLEN] = "";

	if (ss->ss_family == AF_INET)
		inet_ntop(AF_INET, &((const struct sockaddr_in *)ss)->sin_addr, text, sizeof(text));
	else if (ss->ss_family == AF_INET6) {
		const struct in6_addr *a = &((const struct sockaddr_in6 *)ss)->sin6_addr;

		if (IN6_IS_ADDR_V4MAPPED(a))
			inet_ntop(AF_INET, &a->s6_addr[12], text, sizeof(text));
		else
			inet_ntop(AF_INET6, a, text, sizeof(text));
	}
	return g_strdup(text);
}

static char *client_label(ClientKind kind, const char *ip)
{
	g_autofree char *name = NULL;

	if (kind == CLIENT_PHONE)
		return g_strdup("");
	if (kind == CLIENT_HOTSPOT)
		name = hotspot_name(ip);
	return name ? g_strdup_printf("%s (%s)", name, ip) : g_strdup(ip);
}

/* ===================================================================== requests */

static Request *request_new(ClientKind kind)
{
	Request *r = g_new0(Request, 1);

	r->ref = 1;
	r->kind = kind;
	r->listen_fd = -1;
	r->usock = -1;
	r->start = g_get_monotonic_time();
	r->wall = g_get_real_time();
	if (kind != CLIENT_INTERNAL)
		self.requests++;
	return r;
}

static Request *request_ref(Request *r)
{
	r->ref++;
	return r;
}

/* Ends the current attempt: socket, timer, stream entry, cancellable */
static void attempt_end(Request *r)
{
	if (r->usource) {
		g_source_remove(r->usource);
		r->usource = 0;
	}
	if (r->usock >= 0) {
		close(r->usock);
		r->usock = -1;
	}
	if (r->timer) {
		g_source_remove(r->timer);
		r->timer = 0;
	}
	if (r->in_stream && r->cur && r->cur->stream)
		stream_forget(r->cur->stream, r);
	r->in_stream = FALSE;
	if (r->cancel) {
		g_cancellable_cancel(r->cancel);
		g_clear_object(&r->cancel);
	}
	r->attempt++;
}

/* The request no longer counts for its app or device */
static void asker_release(Request *r)
{
	guint n;

	if (!r->asker)
		return;
	n = GPOINTER_TO_UINT(g_hash_table_lookup(self.requests_per, r->asker));
	if (n > 1)
		g_hash_table_insert(self.requests_per, g_strdup(r->asker), GUINT_TO_POINTER(n - 1));
	else
		g_hash_table_remove(self.requests_per, r->asker);
	g_clear_pointer(&r->asker, g_free);
}

static void request_unref(Request *r)
{
	if (--r->ref > 0)
		return;
	asker_release(r);
	g_free(r->client_name);
	g_free(r->app);
	decision_clear(&r->d);
	g_free(r->via);
	if (r->ups)
		g_ptr_array_unref(r->ups);
	if (r->tcp)
		tcp_client_unref(r->tcp);
	g_free(r);
}

/* Done with the request: no more attempts, nothing more to send */
static void request_done(Request *r)
{
	if (r->finished)
		return;
	r->finished = TRUE;
	attempt_end(r);
	if (r->deadline) {
		g_source_remove(r->deadline);
		r->deadline = 0;
	}
	if (r->kind != CLIENT_INTERNAL)
		self.requests--;
	asker_release(r);
	/* Answered or dropped: the TCP client may send its next question */
	if (r->tcp)
		tcp_request_finished(r->tcp);
	request_unref(r);
}

static void send_to_client(Request *r, GByteArray *msg)
{
	if (r->tcp) {
		tcp_client_reply(r->tcp, g_bytes_new(msg->data, msg->len));
		return;
	}
	if (r->listen_fd < 0)
		return;
	{
		struct iovec iov = {msg->data, msg->len};
		union {
			struct cmsghdr align;
			guint8 buf[CMSG_SPACE(sizeof(struct in6_pktinfo))];
		} control;
		struct msghdr mh = {
			.msg_name = &r->peer,
			.msg_namelen = r->peer_len,
			.msg_iov = &iov,
			.msg_iovlen = 1,
		};

		/* Answer from the address the question went to (after the redirect: the interface's) */
		if (r->has_pktinfo) {
			struct cmsghdr *cm;

			memset(&control, 0, sizeof(control));
			mh.msg_control = control.buf;
			mh.msg_controllen = CMSG_SPACE(r->pktinfo_len);
			cm = CMSG_FIRSTHDR(&mh);
			cm->cmsg_level = r->pktinfo_level;
			cm->cmsg_type = r->pktinfo_type;
			cm->cmsg_len = CMSG_LEN(r->pktinfo_len);
			memcpy(CMSG_DATA(cm), r->pktinfo, r->pktinfo_len);
		}
		if (sendmsg(r->listen_fd, &mh, MSG_DONTWAIT) < 0)
			g_debug("Cannot answer: %s", g_strerror(errno));
	}
}

static gsize client_max_size(const Request *r)
{
	if (r->tcp)
		return 65535;
	return r->q.edns ? MIN(r->q.udp_size, 4096) : 512;
}

/* Logs and counts a question we answered */
static void request_log(Request *r, const char *answer, const char *upstream, int rcode, gboolean cached)
{
	LogEntry e = {0};

	if (r->kind == CLIENT_INTERNAL)
		return;
	e.time = r->wall;
	e.name = g_strdup(r->q.name);
	e.type = r->q.type;
	e.kind = r->kind;
	e.client = g_strdup(r->client_name);
	e.app = g_strdup(r->app);
	e.status = r->d.status;
	e.reason = g_strdup(r->d.reason);
	e.via = g_strdup(r->via);
	e.answer = g_strdup(answer);
	e.upstream = g_strdup(upstream);
	e.duration = (guint32)MIN(g_get_monotonic_time() - r->start, G_MAXUINT32);
	e.rcode = rcode < 0 ? DNS_RCODE_SERVFAIL : rcode;
	e.cached = cached;
	e.odd = r->q.odd_name || strlen(r->q.name) > STATS_NAME_MAX;
	log_add(&e);
}

static void answer_blocked(Request *r)
{
	g_autoptr(GByteArray) reply = NULL;
	int rcode = DNS_RCODE_NOERROR;
	gboolean null_ip = TRUE;

	if (r->d.status == STATUS_BLOCKED_SPECIAL) {
		rcode = DNS_RCODE_NXDOMAIN;
	} else if (!strcmp(self.s.block_mode, "nxdomain")) {
		rcode = DNS_RCODE_NXDOMAIN;
	} else if (!strcmp(self.s.block_mode, "refused")) {
		rcode = DNS_RCODE_REFUSED;
		null_ip = FALSE;
	}
	if (r->kind == CLIENT_INTERNAL) {
		if (r->done)
			r->done(r, NULL, 0, r->done_data);
	} else {
		reply = dns_build_reply(&r->q, rcode, null_ip, BLOCK_TTL);
		send_to_client(r, reply);
	}
	request_log(r, rcode == DNS_RCODE_NOERROR && null_ip &&
				       (r->q.type == DNS_TYPE_A || r->q.type == DNS_TYPE_AAAA)
			       ? (r->q.type == DNS_TYPE_A ? "0.0.0.0" : "::")
			       : "",
		    NULL, rcode, FALSE);
	request_done(r);
}

static void answer_fail(Request *r, int rcode)
{
	if (r->kind == CLIENT_INTERNAL) {
		if (r->done)
			r->done(r, NULL, 0, r->done_data);
	} else {
		g_autoptr(GByteArray) reply = dns_build_reply(&r->q, rcode, FALSE, 0);

		send_to_client(r, reply);
	}
	if (rcode == DNS_RCODE_SERVFAIL && r->d.status != STATUS_UNFILTERED) {
		decision_clear(&r->d);
		r->d.status = STATUS_FAILED;
	}
	request_log(r, NULL, r->cur ? r->cur->spec : NULL, rcode, FALSE);
	request_done(r);
}

/* A stored or received answer for the client: CNAMEs checked, made to fit, sent */
static void deliver(Request *r, const guint8 *msg, gsize len, const char *upstream, gboolean cached, guint32 age)
{
	DnsAnswer a;
	g_autoptr(GByteArray) reply = NULL;

	if (!dns_parse_answer(msg, len, &a)) {
		dns_answer_clear(&a);
		answer_fail(r, DNS_RCODE_SERVFAIL);
		return;
	}
	/* Trackers hidden behind a CNAME; only when the name itself was not allowed on purpose */
	if (r->kind != CLIENT_INTERNAL && r->d.status == STATUS_PASSED) {
		for (guint i = 0; i < a.cnames->len; i++) {
			const char *target = g_ptr_array_index(a.cnames, i);
			Decision d = decide(target, r->app);

			if (status_blocked(d.status)) {
				decision_clear(&r->d);
				r->d = d;
				r->via = g_strdup(target);
				dns_answer_clear(&a);
				answer_blocked(r);
				return;
			}
			decision_clear(&d);
		}
	}
	if (r->kind == CLIENT_INTERNAL) {
		if (r->done)
			r->done(r, msg, len, r->done_data);
	} else {
		reply = g_byte_array_sized_new(len);
		g_byte_array_append(reply, msg, len);
		if (dns_prepare_reply(reply, r->q.id, age, r->q.edns, client_max_size(r)))
			send_to_client(r, reply);
	}
	request_log(r, a.summary->str, upstream, a.rcode, cached);
	dns_answer_clear(&a);
	request_done(r);
}

static void request_finish_answer(Request *r, const guint8 *msg, gsize len, const char *upstream)
{
	DnsAnswer a;
	g_autofree guint8 *plain = NULL;

	if (r->finished)
		return;
	/* "DNSSEC validated" (AD) means something only from a server we reach authenticated (DoT,
	 * DoH). A plain server's AD bit could come from anyone on the way, and the stub's trust-ad
	 * makes glibc believe it: it goes (also for the cache). */
	if (r->cur && r->cur->proto == PROTO_UDP && len >= DNS_HEADER_SIZE && (dns_get16(msg + 2) & DNS_FLAG_AD)) {
		plain = g_memdup2(msg, len);
		dns_put16(plain + 2, dns_get16(plain + 2) & ~DNS_FLAG_AD);
		msg = plain;
	}
	if (!dns_parse_answer(msg, len, &a) || !dns_answer_matches(&a, &r->q)) {
		/* Not an answer to this question: as if the server had not answered */
		dns_answer_clear(&a);
		if (r->cur) {
			r->cur->failures++;
			r->cur->failed_at = mono_s();
		}
		attempt_end(r);
		request_next(r);
		return;
	}
	if (a.rcode == DNS_RCODE_SERVFAIL || a.rcode == DNS_RCODE_REFUSED || a.rcode == DNS_RCODE_NOTIMP) {
		dns_answer_clear(&a);
		if (r->cur) {
			r->cur->failures++;
			r->cur->failed_at = mono_s();
		}
		attempt_end(r);
		/* The last server's answer stands if no other is left */
		if (r->next >= r->ups->len) {
			deliver(r, msg, len, upstream, FALSE, 0);
			return;
		}
		request_next(r);
		return;
	}
	if ((a.flags & DNS_FLAG_TC) && r->cur && r->cur->proto == PROTO_UDP && !r->tcp_retry) {
		/* Truncated over UDP: same server over TCP */
		dns_answer_clear(&a);
		attempt_end(r);
		r->tcp_retry = TRUE;
		r->next--;
		request_next(r);
		return;
	}
	if (r->cur)
		r->cur->failures = 0;
	if (!r->no_cache)
		cache_put(&r->q, msg, len, &a);
	dns_answer_clear(&a);
	attempt_end(r);
	deliver(r, msg, len, upstream, FALSE, 0);
}

static gboolean on_attempt_timeout(gpointer user_data)
{
	Request *r = user_data;

	r->timer = 0;
	if (r->cur) {
		r->cur->failures++;
		r->cur->failed_at = mono_s();
	}
	attempt_end(r);
	request_next(r);
	return G_SOURCE_REMOVE;
}

static gboolean on_deadline(gpointer user_data)
{
	Request *r = user_data;

	r->deadline = 0;
	attempt_end(r);
	answer_fail(r, DNS_RCODE_SERVFAIL);
	return G_SOURCE_REMOVE;
}

static gboolean on_udp_answer(int fd, GIOCondition cond, gpointer user_data)
{
	Request *r = user_data;
	guint8 buf[65536];
	ssize_t n = recv(fd, buf, sizeof(buf), MSG_DONTWAIT);

	if (n < 0) {
		if (errno == EAGAIN || errno == EINTR)
			return G_SOURCE_CONTINUE;
		/* ICMP unreachable comes back as ECONNREFUSED on a connected socket: next server */
		r->usource = 0;
		if (r->cur) {
			r->cur->failures++;
			r->cur->failed_at = mono_s();
		}
		attempt_end(r);
		request_next(r);
		return G_SOURCE_REMOVE;
	}
	/* Not our ID: someone guessing, or a late answer to an earlier attempt */
	if (n < DNS_HEADER_SIZE || dns_get16(buf) != r->up_id)
		return G_SOURCE_CONTINUE;
	r->usource = 0;
	request_finish_answer(r, buf, n, r->cur->spec);
	return G_SOURCE_REMOVE;
}

static gboolean udp_attempt(Request *r, GByteArray *query)
{
	Upstream *u = r->cur;
	struct sockaddr_storage ss;
	int fd;

	if (!g_socket_address_to_native(u->addr, &ss, sizeof(ss), NULL))
		return FALSE;
	fd = socket(ss.ss_family, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
	if (fd < 0)
		return FALSE;
	/* A network server is asked through its own interface (mobile DNS works only there) */
	if (u->iface && setsockopt(fd, SOL_SOCKET, SO_BINDTODEVICE, u->iface, strlen(u->iface)) < 0)
		g_debug("Cannot bind to %s: %s", u->iface, g_strerror(errno));
	if (connect(fd, (struct sockaddr *)&ss, g_socket_address_get_native_size(u->addr)) < 0 ||
	    send(fd, query->data, query->len, 0) < 0) {
		close(fd);
		return FALSE;
	}
	r->usock = fd;
	r->usource = g_unix_fd_add(fd, G_IO_IN | G_IO_ERR, on_udp_answer, r);
	r->timer = g_timeout_add(UDP_TIMEOUT_MS, on_attempt_timeout, r);
	return TRUE;
}

typedef struct {
	Request *r;
	guint attempt;
	SoupMessage *msg;
	GInputStream *in;
	/* One byte more than a DNS message may have: filled up means too long */
	guint8 buf[DOH_MAX_BYTES + 1];
} AttemptRef;

static void attempt_ref_free(AttemptRef *ar)
{
	request_unref(ar->r);
	g_clear_object(&ar->msg);
	g_clear_object(&ar->in);
	g_free(ar);
}

static void https_failed(Request *r, const char *why)
{
	g_debug("%s: %s", r->cur->spec, why);
	r->cur->failures++;
	r->cur->failed_at = mono_s();
	attempt_end(r);
	request_next(r);
}

static void on_https_body(GObject *source, GAsyncResult *res, gpointer user_data)
{
	AttemptRef *ar = user_data;
	Request *r = ar->r;
	g_autoptr(GError) error = NULL;
	gsize len = 0;
	gboolean ok = g_input_stream_read_all_finish(G_INPUT_STREAM(source), res, &len, &error);

	if (!r->finished && ar->attempt == r->attempt) {
		if (!ok)
			https_failed(r, error->message);
		else if (len > DOH_MAX_BYTES)
			https_failed(r, "answer too long");
		else {
			/* DoH sends ID 0 (RFC 8484); ours goes back in for the match */
			if (len >= 2)
				dns_put16(ar->buf, r->up_id);
			request_finish_answer(r, ar->buf, len, r->cur->spec);
		}
	}
	attempt_ref_free(ar);
}

static void on_https_answer(GObject *source, GAsyncResult *res, gpointer user_data)
{
	AttemptRef *ar = user_data;
	Request *r = ar->r;
	g_autoptr(GError) error = NULL;

	ar->in = soup_session_send_finish(SOUP_SESSION(source), res, &error);
	if (r->finished || ar->attempt != r->attempt) {
		attempt_ref_free(ar);
		return;
	}
	if (!ar->in || soup_message_get_status(ar->msg) != SOUP_STATUS_OK) {
		https_failed(r, error ? error->message : "HTTP error");
		attempt_ref_free(ar);
		return;
	}
	/* Bounded: a server can't make us hold more than one DNS message */
	g_input_stream_read_all_async(ar->in, ar->buf, sizeof(ar->buf), G_PRIORITY_DEFAULT, r->cancel, on_https_body, ar);
}

static SoupSession *soup(void)
{
	if (!self.soup)
		self.soup = soup_session_new_with_options("timeout", 15, "user-agent", "plinius-dnsfilter/0.1", NULL);
	return self.soup;
}

static gboolean https_attempt(Request *r, GByteArray *query)
{
	g_autoptr(SoupMessage) msg = soup_message_new("POST", r->cur->url);
	g_autoptr(GBytes) body = NULL;
	AttemptRef *ar;

	if (!msg)
		return FALSE;
	dns_put16(query->data, 0);
	body = g_bytes_new(query->data, query->len);
	soup_message_set_request_body_from_bytes(msg, "application/dns-message", body);
	soup_message_headers_replace(soup_message_get_request_headers(msg), "Accept", "application/dns-message");
	r->cancel = g_cancellable_new();
	ar = g_new0(AttemptRef, 1);
	ar->r = request_ref(r);
	ar->attempt = r->attempt;
	ar->msg = g_object_ref(msg);
	soup_session_send_async(soup(), msg, G_PRIORITY_DEFAULT, r->cancel, on_https_answer, ar);
	r->timer = g_timeout_add(STREAM_TIMEOUT_MS, on_attempt_timeout, r);
	return TRUE;
}

/* ---- Streams: DNS over TLS, and TCP for truncated answers ----
 * A stream may outlive its server (the settings changed, a test ended): every pending callback
 * holds a reference, and a closed stream ignores what comes back. */

/* Queries of requests that wait for the connection */
static GHashTable *pending_queries;

static Stream *stream_ref(Stream *st)
{
	st->ref++;
	return st;
}

static void stream_unref(Stream *st)
{
	if (--st->ref > 0)
		return;
	g_hash_table_unref(st->inflight);
	g_queue_clear_full(&st->writes, (GDestroyNotify)g_bytes_unref);
	g_clear_object(&st->cancel);
	g_clear_object(&st->io);
	g_clear_object(&st->addr);
	g_free(st->body);
	g_free(st->spec);
	g_free(st->host);
	g_free(st);
}

static void stream_failed(Stream *st)
{
	if (st->up) {
		st->up->failures++;
		st->up->failed_at = mono_s();
	}
}

static void stream_close(Stream *st, const char *why)
{
	GHashTableIter it;
	gpointer k, v;
	g_autoptr(GPtrArray) failed = NULL;

	if (st->closing)
		return;
	st->closing = TRUE;
	stream_ref(st);
	if (st->idle) {
		g_source_remove(st->idle);
		st->idle = 0;
	}
	if (st->cancel) {
		g_cancellable_cancel(st->cancel);
		g_clear_object(&st->cancel);
	}
	if (st->io) {
		g_io_stream_close(st->io, NULL, NULL);
		g_clear_object(&st->io);
	}
	st->connecting = FALSE;
	st->writing = FALSE;
	st->generation++;
	g_queue_clear_full(&st->writes, (GDestroyNotify)g_bytes_unref);
	g_clear_pointer(&st->body, g_free);
	/* Everyone waiting moves on to the next server */
	failed = g_ptr_array_new_with_free_func((GDestroyNotify)request_unref);
	g_hash_table_iter_init(&it, st->inflight);
	while (g_hash_table_iter_next(&it, &k, &v))
		g_ptr_array_add(failed, request_ref(v));
	while (!g_queue_is_empty(&st->waiting)) {
		Request *r = g_queue_pop_head(&st->waiting);

		g_hash_table_remove(pending_queries, r);
		g_ptr_array_add(failed, request_ref(r));
	}
	g_hash_table_remove_all(st->inflight);
	if (failed->len)
		g_debug("%s: %s", st->spec, why);
	for (guint i = 0; i < failed->len; i++) {
		Request *r = g_ptr_array_index(failed, i);

		r->in_stream = FALSE;
		if (r->finished)
			continue;
		stream_failed(st);
		attempt_end(r);
		request_next(r);
	}
	st->closing = FALSE;
	stream_unref(st);
}

static void stream_forget(Stream *st, Request *r)
{
	g_hash_table_remove(st->inflight, GUINT_TO_POINTER(r->up_id));
	if (g_queue_remove(&st->waiting, r))
		g_hash_table_remove(pending_queries, r);
}

/* The server is gone: close what is open, the stream lives on until its callbacks are done */
static void stream_detach(Stream *st)
{
	st->up = NULL;
	stream_close(st, "server removed");
	stream_unref(st);
}

static gboolean on_stream_idle(gpointer user_data)
{
	Stream *st = user_data;

	st->idle = 0;
	if (!g_hash_table_size(st->inflight) && g_queue_is_empty(&st->waiting))
		stream_close(st, "idle");
	return G_SOURCE_REMOVE;
}

static void stream_kick_idle(Stream *st)
{
	if (st->idle)
		g_source_remove(st->idle);
	st->idle = g_timeout_add_seconds(st->tls ? STREAM_IDLE_S : 2, on_stream_idle, st);
}

/* Data of one async call: the stream and the connection it was made on */
typedef struct {
	Stream *st;
	guint generation;
} StreamOp;

static StreamOp *stream_op(Stream *st)
{
	StreamOp *op = g_new(StreamOp, 1);

	op->st = stream_ref(st);
	op->generation = st->generation;
	return op;
}

/* Takes the op; TRUE if the stream is still the connection the call was made on */
static gboolean stream_op_end(StreamOp *op, Stream **st)
{
	gboolean current = op->generation == op->st->generation;

	*st = op->st;
	g_free(op);
	return current;
}

static void stream_write_next(Stream *st);

static void on_stream_written(GObject *source, GAsyncResult *res, gpointer user_data)
{
	Stream *st;
	gboolean current = stream_op_end(user_data, &st);
	g_autoptr(GError) error = NULL;
	gboolean ok = g_output_stream_write_all_finish(G_OUTPUT_STREAM(source), res, NULL, &error);

	if (current) {
		st->writing = FALSE;
		if (!ok) {
			stream_close(st, error->message);
		} else {
			g_bytes_unref(g_queue_pop_head(&st->writes));
			stream_write_next(st);
		}
	}
	stream_unref(st);
}

static void stream_write_next(Stream *st)
{
	GBytes *b;

	if (st->writing || !st->io || !(b = g_queue_peek_head(&st->writes)))
		return;
	st->writing = TRUE;
	g_output_stream_write_all_async(g_io_stream_get_output_stream(st->io), g_bytes_get_data(b, NULL),
					g_bytes_get_size(b), G_PRIORITY_DEFAULT, st->cancel, on_stream_written,
					stream_op(st));
}

static void stream_read_length(Stream *st);

static void on_stream_body(GObject *source, GAsyncResult *res, gpointer user_data)
{
	Stream *st;
	gboolean current = stream_op_end(user_data, &st);
	g_autoptr(GError) error = NULL;
	gsize n = 0;
	gboolean ok = g_input_stream_read_all_finish(G_INPUT_STREAM(source), res, &n, &error);

	if (!current) {
		stream_unref(st);
		return;
	}
	if (!ok || n != st->body_len) {
		stream_close(st, error ? error->message : "closed");
	} else {
		guint8 *body = g_steal_pointer(&st->body);
		gsize len = st->body_len;
		Request *r = g_hash_table_lookup(st->inflight, GUINT_TO_POINTER(dns_get16(body)));
		guint generation = st->generation;

		if (r) {
			g_hash_table_remove(st->inflight, GUINT_TO_POINTER(r->up_id));
			r->in_stream = FALSE;
			request_finish_answer(r, body, len, st->spec);
		}
		g_free(body);
		/* The answer may have ended everything (the last request of a test server) */
		if (generation == st->generation && st->io) {
			stream_kick_idle(st);
			stream_read_length(st);
		}
	}
	stream_unref(st);
}

static void on_stream_length(GObject *source, GAsyncResult *res, gpointer user_data)
{
	Stream *st;
	gboolean current = stream_op_end(user_data, &st);
	g_autoptr(GError) error = NULL;
	gsize n = 0;
	gboolean ok = g_input_stream_read_all_finish(G_INPUT_STREAM(source), res, &n, &error);

	if (current) {
		if (!ok || n != 2) {
			stream_close(st, error ? error->message : "closed by the server");
		} else if ((st->body_len = dns_get16(st->len_buf)) < DNS_HEADER_SIZE) {
			stream_close(st, "bad length");
		} else {
			st->body = g_malloc(st->body_len);
			g_input_stream_read_all_async(g_io_stream_get_input_stream(st->io), st->body, st->body_len,
						      G_PRIORITY_DEFAULT, st->cancel, on_stream_body, stream_op(st));
		}
	}
	stream_unref(st);
}

static void stream_read_length(Stream *st)
{
	g_input_stream_read_all_async(g_io_stream_get_input_stream(st->io), st->len_buf, 2, G_PRIORITY_DEFAULT, st->cancel,
				      on_stream_length, stream_op(st));
}

static void stream_queue(Stream *st, Request *r, GByteArray *query)
{
	guint8 len[2];
	GByteArray *framed = g_byte_array_sized_new(query->len + 2);

	dns_put16(len, query->len);
	g_byte_array_append(framed, len, 2);
	g_byte_array_append(framed, query->data, query->len);
	g_queue_push_tail(&st->writes, g_byte_array_free_to_bytes(framed));
	g_hash_table_insert(st->inflight, GUINT_TO_POINTER(r->up_id), r);
	r->in_stream = TRUE;
	stream_kick_idle(st);
	stream_write_next(st);
}

static void on_stream_connected(GObject *source, GAsyncResult *res, gpointer user_data)
{
	Stream *st;
	gboolean current = stream_op_end(user_data, &st);
	g_autoptr(GError) error = NULL;
	GSocketConnection *conn = g_socket_client_connect_finish(G_SOCKET_CLIENT(source), res, &error);

	if (!current) {
		if (conn) {
			g_io_stream_close(G_IO_STREAM(conn), NULL, NULL);
			g_object_unref(conn);
		}
		stream_unref(st);
		return;
	}
	st->connecting = FALSE;
	if (!conn) {
		stream_close(st, error->message);
		stream_unref(st);
		return;
	}
	st->io = G_IO_STREAM(conn);
	stream_read_length(st);
	while (!g_queue_is_empty(&st->waiting)) {
		Request *r = g_queue_pop_head(&st->waiting);
		GByteArray *q = g_hash_table_lookup(pending_queries, r);

		r->in_stream = FALSE;
		if (q) {
			stream_queue(st, r, q);
			g_hash_table_remove(pending_queries, r);
		}
	}
	stream_unref(st);
}

static void stream_send_query(Stream *st, Request *r, GByteArray *query)
{
	if (st->io) {
		stream_queue(st, r, query);
		return;
	}
	g_queue_push_tail(&st->waiting, r);
	r->in_stream = TRUE;
	g_hash_table_insert(pending_queries, r, g_byte_array_ref(query));
	if (!st->connecting) {
		g_autoptr(GSocketClient) client = g_socket_client_new();

		st->connecting = TRUE;
		g_clear_object(&st->cancel);
		st->cancel = g_cancellable_new();
		g_socket_client_set_timeout(client, 5);
		if (st->tls) {
			g_socket_client_set_tls(client, TRUE);
			g_socket_client_connect_to_host_async(client, st->host, st->port, st->cancel, on_stream_connected,
							      stream_op(st));
		} else {
			g_socket_client_connect_async(client, G_SOCKET_CONNECTABLE(st->addr), st->cancel,
						      on_stream_connected, stream_op(st));
		}
	}
}

static Stream *upstream_stream(Upstream *u)
{
	if (!u->stream) {
		Stream *st = g_new0(Stream, 1);

		st->ref = 1;
		st->up = u;
		st->tls = u->proto == PROTO_TLS;
		st->spec = g_strdup(u->spec);
		st->host = g_strdup(u->host);
		st->port = u->port;
		st->addr = u->addr ? g_object_ref(u->addr) : NULL;
		st->inflight = g_hash_table_new(NULL, NULL);
		g_queue_init(&st->waiting);
		g_queue_init(&st->writes);
		u->stream = st;
	}
	return u->stream;
}

/* Transaction IDs from the kernel's generator: GLib's Mersenne Twister can be predicted from
 * enough of its output, which a network server sees */
static guint16 random_id(void)
{
	guint16 id;

	if (getrandom(&id, sizeof(id), 0) != sizeof(id))
		id = (guint16)g_random_int_range(0, 0x10000);
	return id;
}

/* Asks the next server; SERVFAIL when none is left */
static void request_next(Request *r)
{
	g_autoptr(GByteArray) query = NULL;

	if (r->finished)
		return;
	while (r->next < r->ups->len) {
		gboolean ok = FALSE;

		r->cur = g_ptr_array_index(r->ups, r->next++);
		r->up_id = random_id();
		g_clear_pointer(&query, g_byte_array_unref);
		query = dns_build_query(&r->q, r->up_id, UPSTREAM_UDP_SIZE);
		if (!query)
			break;
		switch (r->cur->proto) {
		case PROTO_UDP:
			if (r->tcp_retry) {
				Stream *st = upstream_stream(r->cur);

				stream_send_query(st, r, query);
				r->timer = g_timeout_add(STREAM_TIMEOUT_MS, on_attempt_timeout, r);
				ok = TRUE;
			} else {
				ok = udp_attempt(r, query);
			}
			break;
		case PROTO_TLS:
			stream_send_query(upstream_stream(r->cur), r, query);
			r->timer = g_timeout_add(STREAM_TIMEOUT_MS, on_attempt_timeout, r);
			ok = TRUE;
			break;
		case PROTO_HTTPS:
			ok = https_attempt(r, query);
			break;
		}
		if (ok)
			return;
		r->cur->failures++;
		r->cur->failed_at = mono_s();
		r->tcp_retry = FALSE;
	}
	answer_fail(r, DNS_RCODE_SERVFAIL);
}

/* A question from a client (or ourselves) */
static void request_start(Request *r)
{
	GBytes *cached;
	guint32 age = 0;

	if (r->kind != CLIENT_INTERNAL) {
		r->d = decide(r->q.name, r->app);
		if (status_blocked(r->d.status)) {
			answer_blocked(r);
			return;
		}
	}
	if (!r->no_cache && (cached = cache_get(&r->q, &age))) {
		gsize len;
		const guint8 *data = g_bytes_get_data(cached, &len);

		deliver(r, data, len, NULL, TRUE, age);
		g_bytes_unref(cached);
		return;
	}
	r->ups = pick_upstreams(r->q.name, r->kind == CLIENT_INTERNAL);
	r->deadline = g_timeout_add(DEADLINE_MS, on_deadline, r);
	request_next(r);
}

static void handle_query(Request *r, const guint8 *msg, gsize len)
{
	int rcode;

	if (!dns_parse_query(msg, len, &r->q, &rcode)) {
		if (rcode >= 0 && len >= DNS_HEADER_SIZE) {
			/* Only the header goes back; the question could not be read */
			GByteArray *reply = g_byte_array_sized_new(DNS_HEADER_SIZE);
			guint8 h[DNS_HEADER_SIZE] = {0};

			memcpy(h, msg, 2);
			dns_put16(h + 2, DNS_FLAG_QR | (dns_get16(msg + 2) & DNS_FLAG_RD) | rcode);
			g_byte_array_append(reply, h, sizeof(h));
			send_to_client(r, reply);
			g_byte_array_unref(reply);
		} else if (r->tcp) {
			/* Nothing to answer (a response, not a question): over TCP that ends the
			 * connection (RFC 7766 allows it), so it can't hold its place doing nothing */
			tcp_client_close(r->tcp);
		}
		request_done(r);
		return;
	}
	if (self.requests > REQUESTS_MAX) {
		answer_fail(r, DNS_RCODE_SERVFAIL);
		return;
	}
	if (r->kind != CLIENT_INTERNAL) {
		const char *who = r->app && *r->app ? r->app : r->client_name ? r->client_name : "";
		guint n;

		if (!self.requests_per)
			self.requests_per = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
		n = GPOINTER_TO_UINT(g_hash_table_lookup(self.requests_per, who));
		if (n >= REQUESTS_PER_ASKER) {
			answer_fail(r, DNS_RCODE_SERVFAIL);
			return;
		}
		g_hash_table_insert(self.requests_per, g_strdup(who), GUINT_TO_POINTER(n + 1));
		r->asker = g_strdup(who);
	}
	request_start(r);
}

/* ---- Internal lookups: host names of encrypted servers ---- */

typedef struct {
	GTask *task;
	guint pending;
	GList *addresses;
} Lookup;

static void on_lookup_answer(Request *r, const guint8 *answer, gsize len, gpointer data)
{
	Lookup *lk = data;

	if (answer) {
		g_autoptr(GPtrArray) addrs = dns_answer_addresses(answer, len);

		for (guint i = 0; i < addrs->len; i++) {
			GInetAddress *a = g_inet_address_new_from_string(g_ptr_array_index(addrs, i));

			if (a)
				lk->addresses = g_list_append(lk->addresses, a);
		}
	}
	if (--lk->pending)
		return;
	if (lk->addresses)
		g_task_return_pointer(lk->task, lk->addresses, (GDestroyNotify)g_resolver_free_addresses);
	else
		g_task_return_new_error(lk->task, G_RESOLVER_ERROR, G_RESOLVER_ERROR_NOT_FOUND,
					"No address for %s from the network's DNS",
					(const char *)g_task_get_task_data(lk->task));
	g_object_unref(lk->task);
	g_free(lk);
}

static void internal_lookup(const char *name, guint16 type, Lookup *lk)
{
	Request *r = request_new(CLIENT_INTERNAL);
	g_autoptr(GByteArray) q = dns_build_simple_query(name, type, 0);
	int rcode;

	r->done = on_lookup_answer;
	r->done_data = lk;
	/* The network's plain answer for a server or list host must not reach the phone's apps
	 * from the cache (they get the answer of the own encrypted servers) */
	r->no_cache = TRUE;
	if (!q || !dns_parse_query(q->data, q->len, &r->q, &rcode)) {
		on_lookup_answer(r, NULL, 0, lk);
		request_done(r);
		return;
	}
	request_start(r);
}

/* GResolver for the whole process (TLS servers, DoH, list downloads): the network's servers,
 * never ourselves (that would ask the encrypted server for its own address) */
typedef struct {
	GResolver parent;
} BootstrapResolver;
typedef struct {
	GResolverClass parent_class;
} BootstrapResolverClass;

G_DEFINE_TYPE(BootstrapResolver, bootstrap_resolver, G_TYPE_RESOLVER)

static void bootstrap_lookup_async(GResolver *resolver, const char *hostname, GResolverNameLookupFlags flags,
				   GCancellable *cancellable, GAsyncReadyCallback callback, gpointer user_data)
{
	Lookup *lk = g_new0(Lookup, 1);
	g_autofree char *ascii = g_hostname_to_ascii(hostname);
	g_autofree char *name = ascii ? g_ascii_strdown(ascii, -1) : NULL;
	gboolean v4 = !(flags & G_RESOLVER_NAME_LOOKUP_FLAGS_IPV6_ONLY);
	gboolean v6 = !(flags & G_RESOLVER_NAME_LOOKUP_FLAGS_IPV4_ONLY);

	lk->task = g_task_new(resolver, cancellable, callback, user_data);
	g_task_set_task_data(lk->task, g_strdup(hostname), g_free);
	if (!name) {
		g_task_return_new_error(lk->task, G_RESOLVER_ERROR, G_RESOLVER_ERROR_NOT_FOUND, "Bad name %s", hostname);
		g_object_unref(lk->task);
		g_free(lk);
		return;
	}
	lk->pending = v4 + v6;
	if (v4)
		internal_lookup(name, DNS_TYPE_A, lk);
	if (v6)
		internal_lookup(name, DNS_TYPE_AAAA, lk);
}

static void bootstrap_lookup_async_noflags(GResolver *resolver, const char *hostname, GCancellable *cancellable,
					   GAsyncReadyCallback callback, gpointer user_data)
{
	bootstrap_lookup_async(resolver, hostname, G_RESOLVER_NAME_LOOKUP_FLAGS_DEFAULT, cancellable, callback, user_data);
}

static GList *bootstrap_lookup_finish(GResolver *resolver, GAsyncResult *result, GError **error)
{
	return g_task_propagate_pointer(G_TASK(result), error);
}

static GList *bootstrap_lookup_sync(GResolver *resolver, const char *hostname, GCancellable *cancellable,
				    GError **error)
{
	g_set_error(error, G_RESOLVER_ERROR, G_RESOLVER_ERROR_INTERNAL, "Only asynchronous lookups");
	return NULL;
}

static GList *bootstrap_lookup_sync_flags(GResolver *resolver, const char *hostname, GResolverNameLookupFlags flags,
					  GCancellable *cancellable, GError **error)
{
	return bootstrap_lookup_sync(resolver, hostname, cancellable, error);
}

static char *bootstrap_reverse(GResolver *resolver, GInetAddress *address, GCancellable *cancellable, GError **error)
{
	g_set_error(error, G_RESOLVER_ERROR, G_RESOLVER_ERROR_INTERNAL, "Not supported");
	return NULL;
}

static void bootstrap_resolver_class_init(BootstrapResolverClass *klass)
{
	GResolverClass *rc = G_RESOLVER_CLASS(klass);

	rc->lookup_by_name = bootstrap_lookup_sync;
	rc->lookup_by_name_async = bootstrap_lookup_async_noflags;
	rc->lookup_by_name_finish = bootstrap_lookup_finish;
	rc->lookup_by_name_with_flags = bootstrap_lookup_sync_flags;
	rc->lookup_by_name_with_flags_async = bootstrap_lookup_async;
	rc->lookup_by_name_with_flags_finish = bootstrap_lookup_finish;
	rc->lookup_by_address = bootstrap_reverse;
}

static void bootstrap_resolver_init(BootstrapResolver *self_)
{
}

/* ===================================================================== listeners */

static gboolean on_udp_query(int fd, GIOCondition cond, gpointer user_data)
{
	gboolean shared = GPOINTER_TO_INT(user_data);

	for (int round = 0; round < 32; round++) {
		guint8 buf[4096];
		union {
			struct cmsghdr align;
			guint8 buf[256];
		} control;
		struct iovec iov = {buf, sizeof(buf)};
		struct sockaddr_storage peer;
		struct msghdr mh = {
			.msg_name = &peer,
			.msg_namelen = sizeof(peer),
			.msg_iov = &iov,
			.msg_iovlen = 1,
			.msg_control = control.buf,
			.msg_controllen = sizeof(control.buf),
		};
		ssize_t n = recvmsg(fd, &mh, MSG_DONTWAIT);
		Request *r;
		ClientKind kind = CLIENT_PHONE;
		int ifindex = 0;
		g_autofree char *ip = NULL;

		if (n < 0)
			break;
		r = request_new(CLIENT_PHONE);
		for (struct cmsghdr *cm = CMSG_FIRSTHDR(&mh); cm; cm = CMSG_NXTHDR(&mh, cm)) {
			if (cm->cmsg_level == IPPROTO_IP && cm->cmsg_type == IP_PKTINFO) {
				struct in_pktinfo pi;

				memcpy(&pi, CMSG_DATA(cm), sizeof(pi));
				ifindex = pi.ipi_ifindex;
				/* Reply from the address it went to */
				pi.ipi_spec_dst = pi.ipi_addr;
				pi.ipi_ifindex = 0;
				memcpy(r->pktinfo, &pi, sizeof(pi));
				r->pktinfo_len = sizeof(pi);
				r->pktinfo_level = IPPROTO_IP;
				r->pktinfo_type = IP_PKTINFO;
				r->has_pktinfo = TRUE;
			} else if (cm->cmsg_level == IPPROTO_IPV6 && cm->cmsg_type == IPV6_PKTINFO) {
				struct in6_pktinfo pi;

				memcpy(&pi, CMSG_DATA(cm), sizeof(pi));
				ifindex = pi.ipi6_ifindex;
				memcpy(r->pktinfo, &pi, sizeof(pi));
				r->pktinfo_len = sizeof(pi);
				r->pktinfo_level = IPPROTO_IPV6;
				r->pktinfo_type = IPV6_PKTINFO;
				r->has_pktinfo = TRUE;
			}
		}
		if (shared) {
			/* Only redirected DNS of hotspot and USB clients; the port is open on every
			 * interface, but nobody on Wi-Fi or mobile data gets an answer */
			kind = shared_kind(ifindex);
			if (kind == CLIENT_INTERNAL || !self.s.shared_clients) {
				r->kind = CLIENT_PHONE;
				request_done(r);
				continue;
			}
		} else {
			r->has_pktinfo = FALSE;
		}
		r->kind = kind;
		r->listen_fd = fd;
		memcpy(&r->peer, &peer, mh.msg_namelen);
		r->peer_len = mh.msg_namelen;
		ip = peer_text(&peer);
		r->client_name = client_label(kind, ip);
		/* The phone's own question: which app (its socket is open while it waits) */
		if (kind == CLIENT_PHONE)
			r->app = appid_for_peer(IPPROTO_UDP, &peer);
		handle_query(r, buf, n);
	}
	return G_SOURCE_CONTINUE;
}

static int udp_listen(int family, const char *address, guint16 port, gboolean pktinfo, GError **error)
{
	struct sockaddr_storage ss = {0};
	socklen_t len;
	int fd = socket(family, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0), on = 1;

	if (fd < 0) {
		g_set_error(error, G_IO_ERROR, g_io_error_from_errno(errno), "socket: %s", g_strerror(errno));
		return -1;
	}
	if (family == AF_INET) {
		struct sockaddr_in *sin = (struct sockaddr_in *)&ss;

		sin->sin_family = AF_INET;
		sin->sin_port = htons(port);
		inet_pton(AF_INET, address, &sin->sin_addr);
		len = sizeof(*sin);
		if (pktinfo)
			setsockopt(fd, IPPROTO_IP, IP_PKTINFO, &on, sizeof(on));
	} else {
		struct sockaddr_in6 *sin6 = (struct sockaddr_in6 *)&ss;

		sin6->sin6_family = AF_INET6;
		sin6->sin6_port = htons(port);
		inet_pton(AF_INET6, address, &sin6->sin6_addr);
		len = sizeof(*sin6);
		setsockopt(fd, IPPROTO_IPV6, IPV6_V6ONLY, &on, sizeof(on));
		if (pktinfo)
			setsockopt(fd, IPPROTO_IPV6, IPV6_RECVPKTINFO, &on, sizeof(on));
	}
	if (bind(fd, (struct sockaddr *)&ss, len) < 0) {
		g_set_error(error, G_IO_ERROR, g_io_error_from_errno(errno), "%s port %u: %s", address, port,
			    g_strerror(errno));
		close(fd);
		return -1;
	}
	return fd;
}

/* ---- TCP clients ---- */

static void tcp_client_close(TcpClient *c)
{
	if (c->closed)
		return;
	c->closed = TRUE;
	if (c->idle) {
		g_source_remove(c->idle);
		c->idle = 0;
	}
	if (c->lifetime) {
		g_source_remove(c->lifetime);
		c->lifetime = 0;
	}
	if (c->link) {
		g_queue_delete_link(c->peer ? &self.tcp_shared_q : &self.tcp_phone_q, c->link);
		c->link = NULL;
	}
	g_cancellable_cancel(c->cancel);
	/* With a read pending, g_io_stream_close() leaves the socket open until the last reference
	 * goes, and one more question got an answer: shut it down at once (the fd stays ours until then) */
	g_socket_shutdown(g_socket_connection_get_socket(c->conn), TRUE, TRUE, NULL);
	g_io_stream_close(G_IO_STREAM(c->conn), NULL, NULL);
	if (c->peer) {
		guint n = GPOINTER_TO_UINT(g_hash_table_lookup(self.tcp_peers, c->peer));

		if (n > 1)
			g_hash_table_insert(self.tcp_peers, g_strdup(c->peer), GUINT_TO_POINTER(n - 1));
		else
			g_hash_table_remove(self.tcp_peers, c->peer);
		self.tcp_shared--;
	} else {
		self.tcp_clients--;
	}
}

static void tcp_client_unref(TcpClient *c)
{
	if (--c->ref > 0)
		return;
	tcp_client_close(c);
	g_queue_clear_full(&c->writes, (GDestroyNotify)g_bytes_unref);
	g_free(c->body);
	g_free(c->client_name);
	g_free(c->app);
	g_free(c->peer);
	g_object_unref(c->cancel);
	g_object_unref(c->conn);
	g_free(c);
}

/* Closes a connection that has been quiet for a while; one with questions in flight waits for
 * them (each ends within DEADLINE_MS, so it can't hold its place for long) */
static gboolean on_tcp_idle(gpointer user_data)
{
	TcpClient *c = user_data;

	if (c->pending)
		return G_SOURCE_CONTINUE;
	c->idle = 0;
	tcp_client_close(c);
	return G_SOURCE_REMOVE;
}

static gboolean on_tcp_lifetime(gpointer user_data)
{
	TcpClient *c = user_data;

	c->lifetime = 0;
	g_debug("TCP client %s: open for %u s, closed", c->client_name, TCP_CLIENT_LIFETIME_S);
	tcp_client_close(c);
	return G_SOURCE_REMOVE;
}

static void tcp_kick_idle(TcpClient *c)
{
	if (c->idle)
		g_source_remove(c->idle);
	c->idle = g_timeout_add_seconds(TCP_CLIENT_IDLE_S, on_tcp_idle, c);
}

static void tcp_read_length(TcpClient *c);

/* Reads the next question unless the client has enough in flight or doesn't read its answers */
static void tcp_maybe_read(TcpClient *c)
{
	if (c->closed || c->reading || c->pending >= TCP_PENDING_MAX || c->queued >= TCP_QUEUE_PAUSE)
		return;
	c->reading = TRUE;
	tcp_read_length(c);
}

/* A question of @c is done (answered or dropped) */
static void tcp_request_finished(TcpClient *c)
{
	if (c->pending)
		c->pending--;
	tcp_maybe_read(c);
}

static void tcp_write_next(TcpClient *c);

static void on_tcp_written(GObject *source, GAsyncResult *res, gpointer user_data)
{
	TcpClient *c = user_data;

	c->writing = FALSE;
	if (!g_output_stream_write_all_finish(G_OUTPUT_STREAM(source), res, NULL, NULL)) {
		tcp_client_close(c);
	} else {
		GBytes *b = g_queue_pop_head(&c->writes);

		c->queued -= MIN(c->queued, g_bytes_get_size(b));
		g_bytes_unref(b);
		tcp_write_next(c);
		tcp_maybe_read(c);
	}
	tcp_client_unref(c);
}

static void tcp_write_next(TcpClient *c)
{
	GBytes *b;

	if (c->writing || c->closed || !(b = g_queue_peek_head(&c->writes)))
		return;
	c->writing = TRUE;
	c->ref++;
	g_output_stream_write_all_async(g_io_stream_get_output_stream(G_IO_STREAM(c->conn)), g_bytes_get_data(b, NULL),
					g_bytes_get_size(b), G_PRIORITY_DEFAULT, c->cancel, on_tcp_written, c);
}

static void tcp_client_reply(TcpClient *c, GBytes *msg)
{
	gsize len = g_bytes_get_size(msg);
	GByteArray *framed;
	guint8 l[2];

	if (c->closed) {
		g_bytes_unref(msg);
		return;
	}
	framed = g_byte_array_sized_new(len + 2);
	dns_put16(l, len);
	g_byte_array_append(framed, l, 2);
	g_byte_array_append(framed, g_bytes_get_data(msg, NULL), len);
	g_bytes_unref(msg);
	c->queued += framed->len;
	g_queue_push_tail(&c->writes, g_byte_array_free_to_bytes(framed));
	if (c->queued > TCP_QUEUE_MAX) {
		g_debug("TCP client %s reads no answers: closed", c->client_name);
		tcp_client_close(c);
		return;
	}
	tcp_write_next(c);
	tcp_kick_idle(c);
}

static void on_tcp_body(GObject *source, GAsyncResult *res, gpointer user_data)
{
	TcpClient *c = user_data;
	gsize n = 0;

	if (!g_input_stream_read_all_finish(G_INPUT_STREAM(source), res, &n, NULL) || n != c->body_len) {
		tcp_client_close(c);
	} else {
		Request *r = request_new(c->kind);

		r->tcp = c;
		c->ref++;
		c->pending++;
		r->client_name = g_strdup(c->client_name);
		r->app = g_strdup(c->app);
		handle_query(r, c->body, c->body_len);
		g_clear_pointer(&c->body, g_free);
		c->reading = FALSE;
		tcp_maybe_read(c);
	}
	tcp_client_unref(c);
}

static void on_tcp_length(GObject *source, GAsyncResult *res, gpointer user_data)
{
	TcpClient *c = user_data;
	gsize n = 0;

	if (!g_input_stream_read_all_finish(G_INPUT_STREAM(source), res, &n, NULL) || n != 2 ||
	    (c->body_len = dns_get16(c->len_buf)) < DNS_HEADER_SIZE) {
		tcp_client_close(c);
		tcp_client_unref(c);
		return;
	}
	c->body = g_malloc(c->body_len);
	g_input_stream_read_all_async(g_io_stream_get_input_stream(G_IO_STREAM(c->conn)), c->body, c->body_len,
				      G_PRIORITY_DEFAULT, c->cancel, on_tcp_body, c);
}

static void tcp_read_length(TcpClient *c)
{
	c->ref++;
	g_input_stream_read_all_async(g_io_stream_get_input_stream(G_IO_STREAM(c->conn)), c->len_buf, 2,
				      G_PRIORITY_DEFAULT, c->cancel, on_tcp_length, c);
}

/* Is @peer on the subnet of the interface that has @local? */
static gboolean same_subnet(const struct ifaddrs *i, GInetAddress *peer)
{
	gsize size = g_inet_address_get_native_size(peer);
	const guint8 *p = g_inet_address_to_bytes(peer);
	const guint8 *a = NULL, *m = NULL;

	if (!i->ifa_netmask)
		return FALSE;
	if (i->ifa_addr->sa_family == AF_INET && size == 4) {
		a = (const guint8 *)&((const struct sockaddr_in *)i->ifa_addr)->sin_addr;
		m = (const guint8 *)&((const struct sockaddr_in *)i->ifa_netmask)->sin_addr;
	} else if (i->ifa_addr->sa_family == AF_INET6 && size == 16) {
		a = (const guint8 *)&((const struct sockaddr_in6 *)i->ifa_addr)->sin6_addr;
		m = (const guint8 *)&((const struct sockaddr_in6 *)i->ifa_netmask)->sin6_addr;
	}
	if (!a)
		return FALSE;
	for (gsize k = 0; k < size; k++)
		if ((a[k] & m[k]) != (p[k] & m[k]))
			return FALSE;
	return TRUE;
}

/* Hotspot or USB client over TCP. TCP has no IP_PKTINFO, and Linux accepts packets for any of
 * its addresses on any interface (weak host model): a neighbour on Wi-Fi could connect to the
 * hotspot's address. So the client must also be on that interface's subnet (the firewall drops
 * such packets as well). A forged source address can't complete the handshake. */
static ClientKind kind_of_tcp_client(GInetAddress *local, GInetAddress *peer)
{
	struct ifaddrs *list = NULL;
	ClientKind kind = CLIENT_INTERNAL;
	gsize size = g_inet_address_get_native_size(local);
	const guint8 *bytes = g_inet_address_to_bytes(local);

	if (getifaddrs(&list) < 0)
		return CLIENT_INTERNAL;
	for (struct ifaddrs *i = list; i; i = i->ifa_next) {
		const void *a = NULL;

		if (!i->ifa_addr)
			continue;
		if (i->ifa_addr->sa_family == AF_INET && size == 4)
			a = &((struct sockaddr_in *)i->ifa_addr)->sin_addr;
		else if (i->ifa_addr->sa_family == AF_INET6 && size == 16)
			a = &((struct sockaddr_in6 *)i->ifa_addr)->sin6_addr;
		if (a && !memcmp(a, bytes, size)) {
			if (same_subnet(i, peer))
				kind = shared_kind(if_nametoindex(i->ifa_name));
			break;
		}
	}
	freeifaddrs(list);
	return kind;
}

static gboolean on_tcp_incoming(GSocketService *service, GSocketConnection *conn, GObject *source_object,
				gpointer user_data)
{
	GSocket *sock = g_socket_connection_get_socket(conn);
	g_autoptr(GSocketAddress) local = g_socket_get_local_address(sock, NULL);
	g_autoptr(GSocketAddress) remote = g_socket_get_remote_address(sock, NULL);
	g_autofree char *ip = NULL;
	ClientKind kind = CLIENT_PHONE;
	TcpClient *c;

	if (!local || !remote)
		return TRUE;
	ip = g_inet_address_to_string(g_inet_socket_address_get_address(G_INET_SOCKET_ADDRESS(remote)));
	if (g_inet_socket_address_get_port(G_INET_SOCKET_ADDRESS(local)) == SHARED_PORT) {
		/* Redirected: the local address is the one of the interface the client is on */
		kind = kind_of_tcp_client(g_inet_socket_address_get_address(G_INET_SOCKET_ADDRESS(local)),
					  g_inet_socket_address_get_address(G_INET_SOCKET_ADDRESS(remote)));
		if (kind == CLIENT_INTERNAL || !self.s.shared_clients ||
		    GPOINTER_TO_UINT(g_hash_table_lookup(self.tcp_peers, ip)) >= TCP_PER_PEER_MAX)
			return TRUE;
		if (self.tcp_shared >= TCP_SHARED_MAX && self.tcp_shared_q.head)
			tcp_client_close(self.tcp_shared_q.head->data);
		if (self.tcp_shared >= TCP_SHARED_MAX)
			return TRUE;
	} else {
		if (self.tcp_clients >= TCP_CLIENTS_MAX && self.tcp_phone_q.head)
			tcp_client_close(self.tcp_phone_q.head->data);
		if (self.tcp_clients >= TCP_CLIENTS_MAX)
			return TRUE;
	}
	c = g_new0(TcpClient, 1);
	c->ref = 1;
	c->conn = g_object_ref(conn);
	c->kind = kind;
	c->cancel = g_cancellable_new();
	g_queue_init(&c->writes);
	c->client_name = client_label(kind, ip);
	if (kind == CLIENT_PHONE) {
		struct sockaddr_storage ss;

		if (g_socket_address_to_native(remote, &ss, sizeof(ss), NULL))
			c->app = appid_for_peer(IPPROTO_TCP, &ss);
		self.tcp_clients++;
		g_queue_push_tail(&self.tcp_phone_q, c);
		c->link = self.tcp_phone_q.tail;
	} else {
		c->peer = g_strdup(ip);
		g_hash_table_insert(self.tcp_peers, g_strdup(ip),
				    GUINT_TO_POINTER(GPOINTER_TO_UINT(g_hash_table_lookup(self.tcp_peers, ip)) + 1));
		self.tcp_shared++;
		g_queue_push_tail(&self.tcp_shared_q, c);
		c->link = self.tcp_shared_q.tail;
	}
	c->lifetime = g_timeout_add_seconds(TCP_CLIENT_LIFETIME_S, on_tcp_lifetime, c);
	tcp_kick_idle(c);
	tcp_maybe_read(c);
	tcp_client_unref(c);
	return TRUE;
}

static void listeners_stop(void)
{
	for (guint i = 0; i < self.udp_sources->len; i++)
		g_source_remove(g_array_index(self.udp_sources, guint, i));
	g_array_set_size(self.udp_sources, 0);
	for (guint i = 0; i < self.udp_fds->len; i++)
		close(GPOINTER_TO_INT(g_ptr_array_index(self.udp_fds, i)));
	g_ptr_array_set_size(self.udp_fds, 0);
	if (self.tcp_service) {
		g_socket_service_stop(self.tcp_service);
		g_socket_listener_close(G_SOCKET_LISTENER(self.tcp_service));
		g_clear_object(&self.tcp_service);
	}
	self.listening = FALSE;
}

static gboolean add_udp(int family, const char *address, guint16 port, gboolean shared, GError **error)
{
	int fd = udp_listen(family, address, port, shared, error);
	guint id;

	if (fd < 0)
		return FALSE;
	g_ptr_array_add(self.udp_fds, GINT_TO_POINTER(fd));
	id = g_unix_fd_add(fd, G_IO_IN, on_udp_query, GINT_TO_POINTER(shared));
	g_array_append_val(self.udp_sources, id);
	return TRUE;
}

static gboolean add_tcp(const char *address, guint16 port, GError **error)
{
	g_autoptr(GInetAddress) ia = g_inet_address_new_from_string(address);
	g_autoptr(GSocketAddress) sa = g_inet_socket_address_new(ia, port);
	GSocketFamily family = g_inet_address_get_family(ia);
	g_autoptr(GSocket) sock = g_socket_new(family, G_SOCKET_TYPE_STREAM, G_SOCKET_PROTOCOL_TCP, error);

	if (!sock)
		return FALSE;
	/* IPv6 only: "::" next to "0.0.0.0" on the same port (GIO makes it dual-stack otherwise) */
	if (family == G_SOCKET_FAMILY_IPV6 && !g_socket_set_option(sock, IPPROTO_IPV6, IPV6_V6ONLY, 1, error))
		return FALSE;
	g_socket_set_option(sock, SOL_SOCKET, SO_REUSEADDR, 1, NULL);
	if (!g_socket_bind(sock, sa, TRUE, error) || !g_socket_listen(sock, error))
		return FALSE;
	return g_socket_listener_add_socket(G_SOCKET_LISTENER(self.tcp_service), sock, NULL, error);
}

static gboolean listeners_start(GError **error)
{
	if (self.listening)
		return TRUE;
	self.tcp_service = g_socket_service_new();
	g_signal_connect(self.tcp_service, "incoming", G_CALLBACK(on_tcp_incoming), NULL);
	if (!add_udp(AF_INET, "127.0.0.1", self.port, FALSE, error) || !add_udp(AF_INET6, "::1", self.port, FALSE, error) ||
	    !add_tcp("127.0.0.1", self.port, error) || !add_tcp("::1", self.port, error)) {
		listeners_stop();
		return FALSE;
	}
	/* Redirected DNS of hotspot and USB clients; a failure here leaves the phone filtered */
	if (self.system) {
		g_autoptr(GError) e = NULL;

		if (!add_udp(AF_INET, "0.0.0.0", SHARED_PORT, TRUE, &e) || !add_udp(AF_INET6, "::", SHARED_PORT, TRUE, &e) ||
		    !add_tcp("0.0.0.0", SHARED_PORT, &e) || !add_tcp("::", SHARED_PORT, &e))
			g_warning("Hotspot and USB clients: %s", e->message);
	}
	g_socket_service_start(self.tcp_service);
	self.listening = TRUE;
	return TRUE;
}

/* ===================================================================== system integration */

/* Every program reads resolv.conf: 0644 with our umask of 0077 (g_file_set_contents applies the
 * umask), set on the temporary file before it replaces the old one, so no reader ever sees 0600 */
static void write_world_readable(const char *path, const char *data, gsize len)
{
	g_autofree char *tmp = g_strconcat(path, ".new", NULL);
	int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC | O_NOFOLLOW, 0644);
	gboolean ok = fd >= 0;

	if (ok)
		ok = fchmod(fd, 0644) == 0 && write(fd, data, len) == (gssize)len;
	if (fd >= 0 && close(fd) < 0)
		ok = FALSE;
	if (!ok || rename(tmp, path) < 0) {
		g_warning("Cannot write %s: %s", path, g_strerror(errno));
		g_unlink(tmp);
	}
}

/* /run/plinius-dns/resolv.conf: our stub while filtering, NetworkManager's file otherwise */
static void write_stub(void)
{
	g_autofree char *nm = NULL;
	g_autoptr(GString) out = g_string_new("# Generated by plinius-dnsfilter: the DNS filter answers for this phone.\n"
					      "# NetworkManager's servers are in " NM_RESOLV ".\n");
	g_autofree char *path = g_build_filename(self.link_dir, "stub-resolv.conf", NULL);

	if (g_file_get_contents(NM_RESOLV, &nm, NULL, NULL)) {
		g_auto(GStrv) lines = g_strsplit(nm, "\n", -1);

		/* Search domains so that short names still work */
		for (guint i = 0; lines[i]; i++)
			if (g_str_has_prefix(lines[i], "search ") || g_str_has_prefix(lines[i], "domain "))
				g_string_append_printf(out, "%s\n", lines[i]);
	}
	/* trust-ad: glibc keeps the AD bit (DNSSEC validated) of our answers. We pass it on only
	 * from encrypted servers; answers from plain servers (the network's, or a plain own one)
	 * lose it, since anyone on the path could set it (request_finish_answer). */
	g_string_append(out, "nameserver 127.0.0.1\noptions edns0 trust-ad\n");
	write_world_readable(path, out->str, out->len);
}

static void set_resolv_link(gboolean filter)
{
	g_autofree char *link = g_build_filename(self.link_dir, "resolv.conf", NULL);
	g_autofree char *tmp = g_build_filename(self.link_dir, ".resolv.conf.new", NULL);
	g_autofree char *stub = g_build_filename(self.link_dir, "stub-resolv.conf", NULL);
	const char *target = filter ? stub : NM_RESOLV;
	g_autofree char *current = g_file_read_link(link, NULL);

	if (!self.system)
		return;
	if (filter)
		write_stub();
	if (current && !strcmp(current, target))
		return;
	g_unlink(tmp);
	if (symlink(target, tmp) < 0 || rename(tmp, link) < 0)
		g_warning("Cannot point %s to %s: %s", link, target, g_strerror(errno));
	else
		g_message("DNS of the phone now from %s", filter ? "the filter" : "NetworkManager");
}

/* iptables-legacy: own chains only, hooked in at the top; never flushes anyone else's */
static gboolean ipt(gboolean v6, const char *const *args, gboolean quiet)
{
	const char *argv[32] = {v6 ? "ip6tables-legacy" : "iptables-legacy", "-w"};
	guint n = 2;

	for (guint i = 0; args[i] && n < G_N_ELEMENTS(argv) - 1; i++)
		argv[n++] = args[i];
	argv[n] = NULL;
	return run(argv, quiet);
}

#define CHAIN_PRE "PLINIUS_DNS_PRE"
#define CHAIN_OUT_NAT "PLINIUS_DNS_OUT"
#define CHAIN_FWD "PLINIUS_DNS_FWD"
#define CHAIN_OUT "PLINIUS_DNS_OUT"
#define CHAIN_IN "PLINIUS_DNS_IN"

static const struct {
	const char *table;
	const char *hook;
	const char *chain;
} hooks[] = {
	{"nat", "PREROUTING", CHAIN_PRE},
	{"nat", "OUTPUT", CHAIN_OUT_NAT},
	{"filter", "FORWARD", CHAIN_FWD},
	{"filter", "OUTPUT", CHAIN_OUT},
	{"filter", "INPUT", CHAIN_IN},
};

static void firewall_off(void)
{
	for (int v6 = 0; v6 <= 1; v6++) {
		for (guint i = 0; i < G_N_ELEMENTS(hooks); i++) {
			const char *del[] = {"-t", hooks[i].table, "-D", hooks[i].hook, "-j", hooks[i].chain, NULL};
			const char *flush[] = {"-t", hooks[i].table, "-F", hooks[i].chain, NULL};
			const char *rm[] = {"-t", hooks[i].table, "-X", hooks[i].chain, NULL};

			while (ipt(v6, del, TRUE))
				;
			ipt(v6, flush, TRUE);
			ipt(v6, rm, TRUE);
		}
	}
}

/* Users with a session (logind): their apps get the redirect, not Android or system services */
static GArray *session_uids(void)
{
	GArray *uids = g_array_new(FALSE, FALSE, sizeof(guint32));
	g_autoptr(GVariant) reply = NULL;
	GVariantIter *it;
	guint32 uid;

	if (!self.system_bus)
		return uids;
	reply = g_dbus_connection_call_sync(self.system_bus, "org.freedesktop.login1", "/org/freedesktop/login1",
					    "org.freedesktop.login1.Manager", "ListUsers", NULL, G_VARIANT_TYPE("(a(uso))"),
					    G_DBUS_CALL_FLAGS_NONE, 2000, NULL, NULL);
	if (!reply)
		return uids;
	g_variant_get(reply, "(a(uso))", &it);
	while (g_variant_iter_next(it, "(u&s&o)", &uid, NULL, NULL))
		if (uid >= 1000 && uid != 65534)
			g_array_append_val(uids, uid);
	g_variant_iter_free(it);
	return uids;
}

/* Our chains of one table in one go: iptables-restore --noflush flushes the chains it declares
 * and commits the table at once, so no packet ever sees half of the rules. Rule by rule, each
 * login (also over SSH) left the filter open for up to a second: 5300 reachable from Wi-Fi,
 * DNS over TLS not refused, DNS not redirected */
static gboolean ipt_restore(gboolean v6, const char *input)
{
	const char *argv[] = {v6 ? "ip6tables-legacy-restore" : "iptables-legacy-restore", "-w", "--noflush", NULL};
	g_autoptr(GSubprocess) p = NULL;
	g_autoptr(GError) error = NULL;
	g_autofree char *err = NULL;

	p = g_subprocess_newv(argv, G_SUBPROCESS_FLAGS_STDIN_PIPE | G_SUBPROCESS_FLAGS_STDOUT_SILENCE |
					    G_SUBPROCESS_FLAGS_STDERR_PIPE,
			      &error);
	if (!p || !g_subprocess_communicate_utf8(p, input, NULL, NULL, &err, &error)) {
		g_warning("Cannot run %s: %s", argv[0], error->message);
		return FALSE;
	}
	if (!g_subprocess_get_if_exited(p) || g_subprocess_get_exit_status(p)) {
		g_warning("%s failed, rules unchanged: %s", argv[0], err ? g_strstrip(err) : "");
		return FALSE;
	}
	return TRUE;
}

static void firewall_on(void)
{
	g_autoptr(GArray) uids = session_uids();
	static const char *const shared_ifaces[] = {"ap+", "usb+"};
	static const char *const protos[] = {"udp", "tcp"};

	for (int v6 = 0; v6 <= 1; v6++) {
		g_autoptr(GString) nat = g_string_new("*nat\n:" CHAIN_PRE " - [0:0]\n:" CHAIN_OUT_NAT " - [0:0]\n");
		g_autoptr(GString) filter = g_string_new("*filter\n:" CHAIN_FWD " - [0:0]\n:" CHAIN_OUT " - [0:0]\n:" CHAIN_IN
							 " - [0:0]\n");

		/* 5300 is for hotspot and USB clients only, whose DNS is redirected there: a process of
		 * this phone asking 10.42.0.1:5300 over lo passed for a hotspot client (no rules per app,
		 * a device of its own in the log and the statistics, the TCP places of the clients) */
		for (guint p = 0; p < G_N_ELEMENTS(protos); p++)
			g_string_append_printf(filter, "-A " CHAIN_IN " -i lo -p %s --dport %u -j DROP\n", protos[p],
					       SHARED_PORT);
		g_string_append(filter, "-A " CHAIN_IN " -i lo -j RETURN\n");
		for (guint p = 0; p < G_N_ELEMENTS(protos); p++) {
			const char *reject = p == 1 ? " --reject-with tcp-reset" : "";

			/* Port 5300 listens on every address, but only hotspot and USB clients may reach it
			 * (Linux would take a Wi-Fi neighbour's packets for the hotspot's address too) */
			for (guint k = 0; self.s.shared_clients && k < G_N_ELEMENTS(shared_ifaces); k++)
				g_string_append_printf(filter, "-A " CHAIN_IN " -i %s -p %s --dport %u -j RETURN\n",
						       shared_ifaces[k], protos[p], SHARED_PORT);
			g_string_append_printf(filter, "-A " CHAIN_IN " -p %s --dport %u -j DROP\n", protos[p], SHARED_PORT);
			/* Hotspot and USB: all their DNS to us */
			for (guint k = 0; self.s.shared_clients && k < G_N_ELEMENTS(shared_ifaces); k++)
				g_string_append_printf(nat, "-A " CHAIN_PRE " -i %s -p %s --dport 53 -j REDIRECT --to-ports %u\n",
						       shared_ifaces[k], protos[p], SHARED_PORT);
			/* Apps of the users with a fixed DNS server */
			for (guint k = 0; self.s.redirect_dns && k < uids->len; k++)
				g_string_append_printf(nat,
						       "-A " CHAIN_OUT_NAT " -m owner --uid-owner %u -p %s --dport 53"
						       " -j REDIRECT --to-ports %u\n",
						       g_array_index(uids, guint32, k), protos[p], self.port);
			if (!self.s.block_encrypted)
				continue;
			/* DNS over TLS and QUIC: refused, so the client falls back to plain DNS */
			for (guint k = 0; self.s.shared_clients && k < G_N_ELEMENTS(shared_ifaces); k++)
				g_string_append_printf(filter, "-A " CHAIN_FWD " -i %s -p %s --dport 853 -j REJECT%s\n",
						       shared_ifaces[k], protos[p], reject);
			for (guint k = 0; k < uids->len; k++)
				g_string_append_printf(filter,
						       "-A " CHAIN_OUT " -m owner --uid-owner %u -p %s --dport 853 -j REJECT%s\n",
						       g_array_index(uids, guint32, k), protos[p], reject);
		}
		g_string_append(nat, "COMMIT\n");
		g_string_append(filter, "COMMIT\n");
		/* filter first: 5300 is closed before anything is redirected to it */
		if (!ipt_restore(v6, filter->str) || !ipt_restore(v6, nat->str))
			continue;
		/* Hooked in once, at the top; never flushes anyone else's chains */
		for (guint i = 0; i < G_N_ELEMENTS(hooks); i++) {
			const char *check[] = {"-t", hooks[i].table, "-C", hooks[i].hook, "-j", hooks[i].chain, NULL};
			const char *hook[] = {"-t", hooks[i].table, "-I", hooks[i].hook, "1", "-j", hooks[i].chain, NULL};

			if (!ipt(v6, check, TRUE))
				ipt(v6, hook, FALSE);
		}
	}
}

/* A user logged in or out (logind): the uid rules follow. At boot the filter starts before the
 * first login, so without this the phone's apps would not be redirected until the next change. */
static guint users_source;

static gboolean on_users_changed_idle(gpointer user_data)
{
	users_source = 0;
	if (self.system && self.s.enabled && self.listening) {
		g_message("Users changed: firewall rules renewed");
		firewall_on();
	}
	return G_SOURCE_REMOVE;
}

static void on_users_changed(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			     const char *signal, GVariant *params, gpointer user_data)
{
	/* Coalesce bursts (UserNew + SessionNew at a login) */
	if (!users_source)
		users_source = g_timeout_add(500, on_users_changed_idle, NULL);
}

/* ===================================================================== NetworkManager */

/* Without NetworkManager on the bus (early at boot, a test chroot): the servers of its file, or
 * when testing of /etc/resolv.conf; never loopback (that could be us) */
static void read_resolv_file(const char *path);

static void read_resolv_servers(void)
{
	read_resolv_file(NM_RESOLV);
	if (!self.links->len && !self.system)
		read_resolv_file("/etc/resolv.conf");
}

/* Would asking @text reach ourselves? Loopback, any (0.0.0.0, ::), v4-mapped forms of both, and
 * the phone's own addresses: a VPN, DHCP or router advertisement could hand out such a server,
 * and every question would then come back to us in a loop. */
static gboolean server_is_self(const char *text)
{
	g_autofree char *plain = g_strdup(text);
	g_autoptr(GInetAddress) a = NULL;
	struct ifaddrs *list = NULL;
	gboolean self_addr = FALSE;
	char *cut;

	/* "fe80::1%wlan0", "9.9.9.9#53" */
	if ((cut = strpbrk(plain, "%#")))
		*cut = '\0';
	a = g_inet_address_new_from_string(plain);
	if (!a)
		return FALSE;
	if (g_inet_address_get_family(a) == G_SOCKET_FAMILY_IPV6) {
		const guint8 *b = g_inet_address_to_bytes(a);
		static const guint8 mapped[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff};

		if (!memcmp(b, mapped, sizeof(mapped))) {
			g_autoptr(GInetAddress) v4 = g_inet_address_new_from_bytes(b + 12, G_SOCKET_FAMILY_IPV4);

			g_set_object(&a, v4);
		}
	}
	if (g_inet_address_get_is_loopback(a) || g_inet_address_get_is_any(a))
		return TRUE;
	if (getifaddrs(&list) < 0)
		return FALSE;
	for (struct ifaddrs *i = list; i && !self_addr; i = i->ifa_next) {
		gsize size = g_inet_address_get_native_size(a);
		const void *x = NULL;

		if (!i->ifa_addr)
			continue;
		if (i->ifa_addr->sa_family == AF_INET && size == 4)
			x = &((struct sockaddr_in *)i->ifa_addr)->sin_addr;
		else if (i->ifa_addr->sa_family == AF_INET6 && size == 16)
			x = &((struct sockaddr_in6 *)i->ifa_addr)->sin6_addr;
		self_addr = x && !memcmp(x, g_inet_address_to_bytes(a), size);
	}
	freeifaddrs(list);
	return self_addr;
}

static void read_resolv_file(const char *path)
{
	g_autofree char *text = NULL;
	g_auto(GStrv) lines = NULL;
	NetLink *l;

	if (!g_file_get_contents(path, &text, NULL, NULL))
		return;
	l = g_new0(NetLink, 1);
	l->servers = g_ptr_array_new_with_free_func((GDestroyNotify)upstream_unref);
	l->priority = 1000;
	lines = g_strsplit(text, "\n", -1);
	for (guint i = 0; lines[i]; i++) {
		g_auto(GStrv) t = g_strsplit_set(g_strstrip(lines[i]), " \t", -1);
		Upstream *u;

		if (g_strv_length(t) < 2 || strcmp(t[0], "nameserver") || server_is_self(t[1]))
			continue;
		if ((u = upstream_parse(t[1], NULL)))
			g_ptr_array_add(l->servers, u);
	}
	if (l->servers->len)
		g_ptr_array_add(self.links, l);
	else
		netlink_free(l);
}

static void read_nm_dns_bus(void);

static void read_nm_dns(void)
{
	static char *last;
	g_autoptr(GString) text = g_string_new(NULL);

	read_nm_dns_bus();
	if (!self.links->len)
		read_resolv_servers();
	/* Into the journal when the servers change */
	for (guint i = 0; i < self.links->len; i++) {
		NetLink *l = g_ptr_array_index(self.links, i);

		for (guint k = 0; k < l->servers->len; k++)
			g_string_append_printf(text, "%s%s (%s)", text->len ? ", " : "",
					       ((Upstream *)g_ptr_array_index(l->servers, k))->spec, l->iface ? l->iface : "file");
	}
	if (g_strcmp0(last, text->str)) {
		g_free(last);
		last = g_strdup(text->str);
		g_message("Network DNS servers: %s", text->len ? text->str : "none");
	}
}

static void read_nm_dns_bus(void)
{
	g_autoptr(GVariant) reply = NULL;
	g_autoptr(GVariant) value = NULL;
	GVariantIter it;
	GVariant *entry;

	g_ptr_array_set_size(self.links, 0);
	if (!self.system_bus)
		return;
	reply = g_dbus_connection_call_sync(self.system_bus, "org.freedesktop.NetworkManager",
					    "/org/freedesktop/NetworkManager/DnsManager", "org.freedesktop.DBus.Properties",
					    "Get",
					    g_variant_new("(ss)", "org.freedesktop.NetworkManager.DnsManager", "Configuration"),
					    G_VARIANT_TYPE("(v)"), G_DBUS_CALL_FLAGS_NONE, 2000, NULL, NULL);
	if (!reply)
		return;
	g_variant_get(reply, "(v)", &value);
	if (!g_variant_is_of_type(value, G_VARIANT_TYPE("aa{sv}")))
		return;
	g_variant_iter_init(&it, value);
	while ((entry = g_variant_iter_next_value(&it))) {
		g_autoptr(GVariantDict) d = g_variant_dict_new(entry);
		g_autofree char **servers = NULL;
		NetLink *l = g_new0(NetLink, 1);
		gint32 prio = 0;
		gboolean vpn = FALSE;

		l->servers = g_ptr_array_new_with_free_func((GDestroyNotify)upstream_unref);
		g_variant_dict_lookup(d, "interface", "s", &l->iface);
		g_variant_dict_lookup(d, "priority", "i", &prio);
		g_variant_dict_lookup(d, "vpn", "b", &vpn);
		g_variant_dict_lookup(d, "domains", "^as", &l->domains);
		l->priority = prio;
		l->vpn = vpn;
		if (g_variant_dict_lookup(d, "nameservers", "^a&s", &servers)) {
			for (guint i = 0; servers[i]; i++) {
				Upstream *u = upstream_parse(servers[i], NULL);

				/* Never ourselves (a VPN could hand out 127.0.0.1) */
				if (!u || server_is_self(servers[i])) {
					if (u)
						upstream_unref(u);
					continue;
				}
				u->iface = g_strdup(l->iface);
				g_ptr_array_add(l->servers, u);
			}
		}
		/* An interface without servers (only domains) asks nobody */
		if (l->servers->len)
			g_ptr_array_add(self.links, l);
		else
			netlink_free(l);
		g_variant_unref(entry);
	}
	g_ptr_array_sort(self.links, compare_links);
}

static guint32 nm_uint(const char *property)
{
	g_autoptr(GVariant) reply = NULL;
	g_autoptr(GVariant) value = NULL;

	if (!self.system_bus)
		return 0;
	reply = g_dbus_connection_call_sync(self.system_bus, "org.freedesktop.NetworkManager",
					    "/org/freedesktop/NetworkManager", "org.freedesktop.DBus.Properties", "Get",
					    g_variant_new("(ss)", "org.freedesktop.NetworkManager", property),
					    G_VARIANT_TYPE("(v)"), G_DBUS_CALL_FLAGS_NONE, 2000, NULL, NULL);
	if (!reply)
		return 0;
	g_variant_get(reply, "(v)", &value);
	return g_variant_is_of_type(value, G_VARIANT_TYPE_UINT32) ? g_variant_get_uint32(value) : 0;
}

static GVariant *network_servers_variant(void)
{
	GVariantBuilder b;

	g_variant_builder_init(&b, G_VARIANT_TYPE("as"));
	for (guint i = 0; i < self.links->len; i++) {
		NetLink *l = g_ptr_array_index(self.links, i);

		for (guint k = 0; k < l->servers->len; k++) {
			Upstream *u = g_ptr_array_index(l->servers, k);
			g_autofree char *text = l->iface ? g_strdup_printf("%s (%s)", u->spec, l->iface) : g_strdup(u->spec);

			g_variant_builder_add(&b, "s", text);
		}
	}
	return g_variant_builder_end(&b);
}

static void on_nm_changed(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			  const char *signal, GVariant *params, gpointer user_data)
{
	guint32 old_conn = self.connectivity;

	read_nm_dns();
	self.connectivity = nm_uint("Connectivity");
	self.metered = nm_uint("Metered");
	cache_flush();
	if (self.s.enabled)
		write_stub();
	emit_changed("NetworkServers", network_servers_variant());
	/* Back online: lists that are due get their turn */
	if (old_conn != self.connectivity && self.connectivity >= 3)
		schedule_updates(5);
	update_status();
}

static void on_nm_resolv_changed(GFileMonitor *monitor, GFile *file, GFile *other, GFileMonitorEvent event,
				 gpointer user_data)
{
	if (event != G_FILE_MONITOR_EVENT_CHANGES_DONE_HINT)
		return;
	/* The servers may come from this file (NetworkManager not on the bus) */
	read_nm_dns();
	if (self.s.enabled)
		write_stub();
}

/* ===================================================================== list downloads */

typedef struct {
	char *id;
	SoupMessage *msg;
	GInputStream *in;
	GByteArray *data;
	guint8 buf[65536];
} Download;

static void download_free(Download *dl)
{
	g_free(dl->id);
	g_clear_object(&dl->msg);
	g_clear_object(&dl->in);
	if (dl->data)
		g_byte_array_unref(dl->data);
	g_free(dl);
}

static GVariant *lists_variant(void);

static void list_done(List *l, const char *error)
{
	l->downloading = FALSE;
	l->checked = now_s();
	g_free(l->error);
	l->error = g_strdup(error);
	if (error)
		g_warning("List %s: %s", l->name, error);
	save_lists();
	emit_changed("Lists", lists_variant());
}

static void on_download_read(GObject *source, GAsyncResult *res, gpointer user_data)
{
	Download *dl = user_data;
	List *l = list_find(dl->id);
	g_autoptr(GError) error = NULL;
	gssize n = g_input_stream_read_finish(G_INPUT_STREAM(source), res, &error);

	if (!l) {
		download_free(dl);
		return;
	}
	if (n < 0) {
		list_done(l, error->message);
		download_free(dl);
		return;
	}
	if (n > 0) {
		if (dl->data->len + n > LIST_MAX_BYTES) {
			list_done(l, "The list is larger than 32 MB");
			download_free(dl);
			return;
		}
		g_byte_array_append(dl->data, dl->buf, n);
		g_input_stream_read_async(dl->in, dl->buf, sizeof(dl->buf), G_PRIORITY_LOW, NULL, on_download_read, dl);
		return;
	}
	/* Complete: compile, write, swap in */
	{
		FilterBuilder *b = filter_builder_new();
		FilterSet *set;
		guint unsupported = 0;
		g_autofree char *bin = list_bin_path(l);
		SoupMessageHeaders *h = soup_message_get_response_headers(dl->msg);
		gint64 t0 = g_get_monotonic_time();
		guint kib = dl->data->len / 1024;

		filter_builder_set_max_rules(b, LIST_MAX_RULES);
		filter_builder_add_text(b, (const char *)dl->data->data, dl->data->len);
		/* The builder has copied the names: the download goes before the set is built and saved */
		g_clear_pointer(&dl->data, g_byte_array_unref);
		if (filter_builder_too_many(b)) {
			filter_builder_free(b);
			malloc_trim(0);
			list_done(l, "The list has more than 2 million rules");
			download_free(dl);
			return;
		}
		set = filter_builder_finish(b, &unsupported);
		if (filter_set_size(set) == 0) {
			filter_set_free(set);
			list_done(l, "The download has no rules (not a blocklist?)");
			download_free(dl);
			return;
		}
		if (!filter_set_save(set, bin, &error)) {
			filter_set_free(set);
			list_done(l, error->message);
			download_free(dl);
			return;
		}
		l->rules = filter_set_size(set);
		l->unsupported = unsupported;
		l->updated = now_s();
		g_free(l->etag);
		l->etag = g_strdup(soup_message_headers_get_one(h, "ETag"));
		g_free(l->last_modified);
		l->last_modified = g_strdup(soup_message_headers_get_one(h, "Last-Modified"));
		filter_set_free(set);
		/* The mapped file uses no heap for the rules; the download and the builder's arrays go back
		 * to the system (else ~20 MB stay in the heap after each list) */
		g_message("List %s: %u rules (%u unsupported), %u KiB, %" G_GINT64_FORMAT " ms", l->name, l->rules,
			  l->unsupported, kib, (g_get_monotonic_time() - t0) / 1000);
		list_load_set(l);
		malloc_trim(0);
		list_done(l, NULL);
	}
	download_free(dl);
}

static void on_download_sent(GObject *source, GAsyncResult *res, gpointer user_data)
{
	Download *dl = user_data;
	List *l = list_find(dl->id);
	g_autoptr(GError) error = NULL;
	guint status;

	dl->in = soup_session_send_finish(SOUP_SESSION(source), res, &error);
	if (!l) {
		download_free(dl);
		return;
	}
	if (!dl->in) {
		list_done(l, error->message);
		download_free(dl);
		return;
	}
	status = soup_message_get_status(dl->msg);
	if (status == SOUP_STATUS_NOT_MODIFIED) {
		/* Unchanged, but a missing compiled file means we need it anyway */
		g_autofree char *bin = list_bin_path(l);

		if (g_file_test(bin, G_FILE_TEST_EXISTS)) {
			l->updated = now_s();
			if (!l->set)
				list_load_set(l);
			list_done(l, NULL);
			download_free(dl);
			return;
		}
		g_clear_pointer(&l->etag, g_free);
		g_clear_pointer(&l->last_modified, g_free);
		list_done(l, "Retrying without cache");
		download_free(dl);
		return;
	}
	if (status != SOUP_STATUS_OK) {
		g_autofree char *msg = g_strdup_printf("HTTP %u %s", status, soup_message_get_reason_phrase(dl->msg));

		list_done(l, msg);
		download_free(dl);
		return;
	}
	dl->data = g_byte_array_new();
	g_input_stream_read_async(dl->in, dl->buf, sizeof(dl->buf), G_PRIORITY_LOW, NULL, on_download_read, dl);
}

static void list_download(List *l)
{
	Download *dl;

	if (l->downloading)
		return;
	/* Only https: over http anyone on the way could hand us any list (lists of older versions
	 * may still have such an address) */
	if (!g_str_has_prefix(l->url, "https://")) {
		list_done(l, "Lists need an https:// address");
		return;
	}
	dl = g_new0(Download, 1);
	dl->id = g_strdup(l->id);
	dl->msg = soup_message_new("GET", l->url);
	if (!dl->msg) {
		download_free(dl);
		list_done(l, "Not a web address");
		return;
	}
	if (l->etag && l->set)
		soup_message_headers_replace(soup_message_get_request_headers(dl->msg), "If-None-Match", l->etag);
	if (l->last_modified && l->set)
		soup_message_headers_replace(soup_message_get_request_headers(dl->msg), "If-Modified-Since", l->last_modified);
	l->downloading = TRUE;
	emit_changed("Lists", lists_variant());
	soup_session_send_async(soup(), dl->msg, G_PRIORITY_LOW, NULL, on_download_sent, dl);
}

/* Lists due for an update; @force: all of them, now (the user asked) */
static void update_lists(gboolean force)
{
	/* On mobile data (metered: 1 yes, 3 guessed yes) only lists we don't have yet */
	gboolean metered = self.metered == 1 || self.metered == 3;

	for (guint i = 0; i < self.lists->len; i++) {
		List *l = g_ptr_array_index(self.lists, i);
		gboolean due;

		if (!l->enabled)
			continue;
		due = !l->set || (self.s.update_hours && now_s() - l->updated >= (gint64)self.s.update_hours * 3600);
		/* After a failure, not again right away */
		if (l->error && now_s() - l->checked < LIST_RETRY_S && !force)
			due = FALSE;
		if (l->set && metered && !self.s.update_on_mobile && !force)
			due = FALSE;
		if (due || force)
			list_download(l);
	}
}

static gboolean on_update_timer(gpointer user_data)
{
	self.update_source = 0;
	/* 1 = no network at all; unknown, login page and limited still try */
	if (self.s.enabled && self.connectivity != 1)
		update_lists(FALSE);
	schedule_updates(30 * 60);
	return G_SOURCE_REMOVE;
}

static void schedule_updates(guint delay_s)
{
	if (self.update_source)
		g_source_remove(self.update_source);
	self.update_source = g_timeout_add_seconds(delay_s, on_update_timer, NULL);
}

/* ===================================================================== state */

static void update_status(void)
{
	const char *status;

	if (!self.s.enabled)
		status = "off";
	else if (!self.listening)
		status = "error";
	else if (paused())
		status = "paused";
	else if (!self.links->len && !(self.s.custom && self.custom->len))
		status = "no-network";
	else
		status = "active";
	if (g_strcmp0(status, self.status)) {
		g_free(self.status);
		self.status = g_strdup(status);
		emit_changed("Status", g_variant_new_string(status));
	}
}

static gboolean on_pause_over(gpointer user_data)
{
	self.pause_source = 0;
	self.paused_until = 0;
	save_settings();
	cache_flush();
	emit_changed("PausedUntil", g_variant_new_int64(0));
	update_status();
	return G_SOURCE_REMOVE;
}

static void apply_state(void)
{
	if (self.s.enabled) {
		g_autoptr(GError) error = NULL;

		if (!listeners_start(&error)) {
			g_warning("Cannot listen: %s", error->message);
			g_free(self.last_error);
			self.last_error = g_strdup(error->message);
			set_resolv_link(FALSE);
			if (self.system)
				firewall_off();
		} else {
			set_resolv_link(TRUE);
			if (self.system)
				firewall_on();
			schedule_updates(LIST_FIRST_CHECK_S);
		}
	} else {
		set_resolv_link(FALSE);
		if (self.system)
			firewall_off();
		listeners_stop();
		cache_flush();
	}
	if (self.pause_source) {
		g_source_remove(self.pause_source);
		self.pause_source = 0;
	}
	if (paused())
		self.pause_source = g_timeout_add_seconds(MAX(1, self.paused_until - now_s()), on_pause_over, NULL);
	update_status();
	touch_idle();
}

static gboolean on_idle_exit(gpointer user_data)
{
	self.idle_source = 0;
	if (!self.s.enabled && !g_hash_table_size(self.watchers)) {
		g_message("Filter off and nobody asking: leaving");
		g_main_loop_quit(self.loop);
	}
	return G_SOURCE_REMOVE;
}

static void touch_idle(void)
{
	if (self.idle_source)
		g_source_remove(self.idle_source);
	self.idle_source = 0;
	if (!self.s.enabled)
		self.idle_source = g_timeout_add_seconds(IDLE_EXIT_S, on_idle_exit, NULL);
}

static gboolean on_save_timer(gpointer user_data)
{
	if (self.stats_dirty)
		save_stats();
	if (self.log_dirty && self.s.keep_log)
		save_log();
	return G_SOURCE_CONTINUE;
}

/* ===================================================================== D-Bus */

static const char introspection[] =
	"<node>"
	" <interface name='" INTERFACE "'>"
	"  <property name='Enabled' type='b' access='readwrite'/>"
	"  <property name='Status' type='s' access='read'/>"
	"  <property name='PausedUntil' type='x' access='read'/>"
	"  <property name='BlockMode' type='s' access='readwrite'/>"
	"  <property name='CustomServers' type='b' access='readwrite'/>"
	"  <property name='Servers' type='as' access='readwrite'/>"
	"  <property name='NetworkServers' type='as' access='read'/>"
	"  <property name='LocalViaNetwork' type='b' access='readwrite'/>"
	"  <property name='SharedClients' type='b' access='readwrite'/>"
	"  <property name='RedirectDns' type='b' access='readwrite'/>"
	"  <property name='BlockEncryptedDns' type='b' access='readwrite'/>"
	"  <property name='KeepLog' type='b' access='readwrite'/>"
	"  <property name='LogSize' type='u' access='readwrite'/>"
	"  <property name='StatsDays' type='u' access='readwrite'/>"
	"  <property name='UpdateInterval' type='u' access='readwrite'/>"
	"  <property name='UpdateOnMobileData' type='b' access='readwrite'/>"
	"  <property name='Lists' type='aa{sv}' access='read'/>"
	"  <property name='RuleCount' type='u' access='read'/>"
	"  <method name='Pause'><arg name='seconds' type='u' direction='in'/></method>"
	"  <method name='GetCatalog'><arg name='lists' type='aa{sv}' direction='out'/></method>"
	"  <method name='AddList'>"
	"   <arg name='url' type='s' direction='in'/><arg name='name' type='s' direction='in'/>"
	"   <arg name='id' type='s' direction='out'/>"
	"  </method>"
	"  <method name='RemoveList'><arg name='id' type='s' direction='in'/></method>"
	"  <method name='SetListEnabled'>"
	"   <arg name='id' type='s' direction='in'/><arg name='enabled' type='b' direction='in'/>"
	"  </method>"
	"  <method name='UpdateLists'/>"
	"  <method name='GetRules'><arg name='rules' type='as' direction='out'/></method>"
	"  <method name='AddRule'>"
	"   <arg name='rule' type='s' direction='in'/><arg name='canonical' type='s' direction='out'/>"
	"  </method>"
	"  <method name='RemoveRule'><arg name='rule' type='s' direction='in'/></method>"
	"  <method name='Check'>"
	"   <arg name='name' type='s' direction='in'/>"
	"   <arg name='status' type='u' direction='out'/><arg name='reason' type='s' direction='out'/>"
	"  </method>"
	"  <method name='CheckNames'>"
	"   <arg name='app' type='s' direction='in'/><arg name='names' type='as' direction='in'/>"
	"   <arg name='results' type='a(us)' direction='out'/>"
	"  </method>"
	"  <method name='GetNamesOf'>"
	"   <arg name='days' type='u' direction='in'/><arg name='who' type='s' direction='in'/>"
	"   <arg name='names' type='a(su)' direction='out'/>"
	"  </method>"
	"  <method name='GetAskersOf'>"
	"   <arg name='days' type='u' direction='in'/><arg name='name' type='s' direction='in'/>"
	"   <arg name='who' type='a(su)' direction='out'/>"
	"  </method>"
	"  <method name='GetStats'>"
	"   <arg name='days' type='u' direction='in'/><arg name='stats' type='a{sv}' direction='out'/>"
	"  </method>"
	"  <method name='GetLog'>"
	"   <arg name='before' type='u' direction='in'/><arg name='limit' type='u' direction='in'/>"
	"   <arg name='search' type='s' direction='in'/><arg name='filter' type='u' direction='in'/>"
	"   <arg name='entries' type='a" LOG_TYPE "' direction='out'/>"
	"  </method>"
	"  <method name='GetLogNames'>"
	"   <arg name='offset' type='u' direction='in'/><arg name='limit' type='u' direction='in'/>"
	"   <arg name='search' type='s' direction='in'/><arg name='filter' type='u' direction='in'/>"
	"   <arg name='names' type='a(" LOG_TYPE "uu)' direction='out'/>"
	"  </method>"
	"  <method name='ClearLog'/>"
	"  <method name='ClearStats'/>"
	"  <method name='TestServer'>"
	"   <arg name='server' type='s' direction='in'/>"
	"   <arg name='ok' type='b' direction='out'/><arg name='milliseconds' type='u' direction='out'/>"
	"   <arg name='message' type='s' direction='out'/>"
	"  </method>"
	"  <method name='Watch'/>"
	"  <method name='Unwatch'/>"
	"  <signal name='Logged'><arg name='entries' type='a" LOG_TYPE "'/></signal>"
	" </interface>"
	"</node>";

static GVariant *lists_variant(void)
{
	GVariantBuilder b;

	g_variant_builder_init(&b, G_VARIANT_TYPE("aa{sv}"));
	for (guint i = 0; i < self.lists->len; i++) {
		List *l = g_ptr_array_index(self.lists, i);
		GVariantBuilder d;

		g_variant_builder_init(&d, G_VARIANT_TYPE_VARDICT);
		g_variant_builder_add(&d, "{sv}", "id", g_variant_new_string(l->id));
		g_variant_builder_add(&d, "{sv}", "name", g_variant_new_string(l->name));
		g_variant_builder_add(&d, "{sv}", "url", g_variant_new_string(l->url));
		g_variant_builder_add(&d, "{sv}", "enabled", g_variant_new_boolean(l->enabled));
		g_variant_builder_add(&d, "{sv}", "rules", g_variant_new_uint32(l->rules));
		g_variant_builder_add(&d, "{sv}", "unsupported", g_variant_new_uint32(l->unsupported));
		g_variant_builder_add(&d, "{sv}", "updated", g_variant_new_int64(l->updated));
		g_variant_builder_add(&d, "{sv}", "downloading", g_variant_new_boolean(l->downloading));
		g_variant_builder_add(&d, "{sv}", "catalog", g_variant_new_boolean(catalog_find(l->id) != NULL));
		if (l->error)
			g_variant_builder_add(&d, "{sv}", "error", g_variant_new_string(l->error));
		g_variant_builder_add_value(&b, g_variant_builder_end(&d));
	}
	return g_variant_builder_end(&b);
}

static GVariant *catalog_variant(void)
{
	GVariantBuilder b;

	g_variant_builder_init(&b, G_VARIANT_TYPE("aa{sv}"));
	for (guint i = 0; i < G_N_ELEMENTS(catalog); i++) {
		GVariantBuilder d;

		g_variant_builder_init(&d, G_VARIANT_TYPE_VARDICT);
		g_variant_builder_add(&d, "{sv}", "id", g_variant_new_string(catalog[i].id));
		g_variant_builder_add(&d, "{sv}", "name", g_variant_new_string(catalog[i].name));
		g_variant_builder_add(&d, "{sv}", "url", g_variant_new_string(catalog[i].url));
		g_variant_builder_add(&d, "{sv}", "description", g_variant_new_string(catalog[i].description));
		g_variant_builder_add(&d, "{sv}", "added", g_variant_new_boolean(list_find(catalog[i].id) != NULL));
		g_variant_builder_add_value(&b, g_variant_builder_end(&d));
	}
	return g_variant_builder_end(&b);
}

static GVariant *get_property(const char *name)
{
	Settings *s = &self.s;

	if (!strcmp(name, "Enabled"))
		return g_variant_new_boolean(s->enabled);
	if (!strcmp(name, "Status"))
		return g_variant_new_string(self.status ? self.status : "off");
	if (!strcmp(name, "PausedUntil"))
		return g_variant_new_int64(paused() ? self.paused_until : 0);
	if (!strcmp(name, "BlockMode"))
		return g_variant_new_string(s->block_mode);
	if (!strcmp(name, "CustomServers"))
		return g_variant_new_boolean(s->custom);
	if (!strcmp(name, "Servers"))
		return g_variant_new_strv((const char *const *)s->servers, -1);
	if (!strcmp(name, "NetworkServers"))
		return network_servers_variant();
	if (!strcmp(name, "LocalViaNetwork"))
		return g_variant_new_boolean(s->local_via_network);
	if (!strcmp(name, "SharedClients"))
		return g_variant_new_boolean(s->shared_clients);
	if (!strcmp(name, "RedirectDns"))
		return g_variant_new_boolean(s->redirect_dns);
	if (!strcmp(name, "BlockEncryptedDns"))
		return g_variant_new_boolean(s->block_encrypted);
	if (!strcmp(name, "KeepLog"))
		return g_variant_new_boolean(s->keep_log);
	if (!strcmp(name, "LogSize"))
		return g_variant_new_uint32(s->log_size);
	if (!strcmp(name, "StatsDays"))
		return g_variant_new_uint32(s->stats_days);
	if (!strcmp(name, "UpdateInterval"))
		return g_variant_new_uint32(s->update_hours);
	if (!strcmp(name, "UpdateOnMobileData"))
		return g_variant_new_boolean(s->update_on_mobile);
	if (!strcmp(name, "Lists"))
		return lists_variant();
	if (!strcmp(name, "RuleCount"))
		return g_variant_new_uint32(self.rules->len);
	return NULL;
}

/* Own servers (a DoH address may carry a personal profile id) and lists (a list address may
 * carry a token): only for root and an administrator in front of the phone (check_caller) */
static gboolean private_property(const char *property)
{
	return !strcmp(property, "Servers") || !strcmp(property, "Lists");
}

static void emit_changed(const char *property, GVariant *value)
{
	GVariantBuilder changed;
	GVariant *params;

	g_variant_ref_sink(value);
	if (self.bus) {
		g_variant_builder_init(&changed, G_VARIANT_TYPE_VARDICT);
		g_variant_builder_add(&changed, "{sv}", property, value);
		params = g_variant_new("(sa{sv}as)", INTERFACE, &changed, NULL);
		if (private_property(property))
			emit_to_watchers("org.freedesktop.DBus.Properties", "PropertiesChanged", params);
		else
			g_dbus_connection_emit_signal(self.bus, NULL, OBJECT_PATH, "org.freedesktop.DBus.Properties",
						      "PropertiesChanged", params, NULL);
	}
	g_variant_unref(value);
}

/*
 * Whether the connection @sender belongs to a process in the group wheel. Its groups come from the
 * bus daemon (GetConnectionCredentials, UnixGroupIDs: the groups of the process as it connected),
 * wheel's id from the group database. Anything missing counts as no.
 */
static gboolean caller_in_wheel(const char *sender)
{
	g_autoptr(GVariant) reply = NULL;
	g_autoptr(GVariant) creds = NULL;
	g_autoptr(GVariant) gids = NULL;
	struct group *wheel = getgrnam("wheel");
	gid_t wheel_gid;
	const guint32 *ids;
	gsize n;

	if (!wheel)
		return FALSE;
	wheel_gid = wheel->gr_gid;
	reply = g_dbus_connection_call_sync(self.bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
					    "org.freedesktop.DBus", "GetConnectionCredentials", g_variant_new("(s)", sender),
					    G_VARIANT_TYPE("(a{sv})"), G_DBUS_CALL_FLAGS_NONE, 2000, NULL, NULL);
	if (!reply)
		return FALSE;
	creds = g_variant_get_child_value(reply, 0);
	gids = g_variant_lookup_value(creds, "UnixGroupIDs", G_VARIANT_TYPE("au"));
	if (!gids)
		return FALSE;
	ids = g_variant_get_fixed_array(gids, &n, sizeof(guint32));
	for (gsize i = 0; i < n; i++)
		if (ids[i] == wheel_gid)
			return TRUE;
	return FALSE;
}

/*
 * Root, or the user of the active session on seat0 if it is an administrator (group wheel). The
 * filter acts for the whole phone and the log is everyone's, so another account in front of the
 * phone is refused too (attack review 2026-11-18, L15).
 */
static gboolean check_caller(const char *sender, GError **error)
{
	g_autoptr(GVariant) reply = NULL;
	g_autoptr(GVariant) session = NULL;
	g_autoptr(GVariant) user = NULL;
	guint32 caller, uid;
	const char *path;

	if (!self.system)
		return TRUE;
	reply = g_dbus_connection_call_sync(self.bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
					    "org.freedesktop.DBus", "GetConnectionUnixUser", g_variant_new("(s)", sender),
					    G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, -1, NULL, error);
	if (!reply)
		return FALSE;
	g_variant_get(reply, "(u)", &caller);
	if (caller == 0)
		return TRUE;
	g_clear_pointer(&reply, g_variant_unref);
	reply = g_dbus_connection_call_sync(self.bus, "org.freedesktop.login1", "/org/freedesktop/login1/seat/seat0",
					    "org.freedesktop.DBus.Properties", "Get",
					    g_variant_new("(ss)", "org.freedesktop.login1.Seat", "ActiveSession"),
					    G_VARIANT_TYPE("(v)"), G_DBUS_CALL_FLAGS_NONE, 2000, NULL, NULL);
	if (reply) {
		g_variant_get(reply, "(v)", &session);
		if (g_variant_is_of_type(session, G_VARIANT_TYPE("(so)"))) {
			g_variant_get(session, "(&s&o)", NULL, &path);
			g_clear_pointer(&reply, g_variant_unref);
			reply = g_dbus_connection_call_sync(self.bus, "org.freedesktop.login1", path,
							    "org.freedesktop.DBus.Properties", "Get",
							    g_variant_new("(ss)", "org.freedesktop.login1.Session", "User"),
							    G_VARIANT_TYPE("(v)"), G_DBUS_CALL_FLAGS_NONE, 2000, NULL, NULL);
			if (reply) {
				g_variant_get(reply, "(v)", &user);
				if (g_variant_is_of_type(user, G_VARIANT_TYPE("(uo)"))) {
					g_variant_get(user, "(uo)", &uid, NULL);
					if (uid == caller && caller_in_wheel(sender))
						return TRUE;
				}
			}
		}
	}
	g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED, "Only an administrator in front of the phone can do this");
	return FALSE;
}

static void unwatch_name(gpointer id)
{
	g_bus_unwatch_name(GPOINTER_TO_UINT(id));
}

static void on_watcher_vanished(GDBusConnection *bus, const char *name, gpointer user_data)
{
	g_hash_table_remove(self.watchers, name);
	touch_idle();
}

typedef struct {
	GDBusMethodInvocation *inv;
	gint64 start;
} TestCall;

static void on_test_answer(Request *r, const guint8 *answer, gsize len, gpointer data)
{
	TestCall *t = data;
	guint32 ms = (guint32)((g_get_monotonic_time() - t->start) / 1000);

	if (answer) {
		DnsAnswer a;
		gboolean ok = dns_parse_answer(answer, len, &a) && a.rcode == DNS_RCODE_NOERROR && a.answers > 0;

		g_dbus_method_invocation_return_value(t->inv, g_variant_new("(bus)", ok, ms, ok ? a.summary->str : "No answer"));
		dns_answer_clear(&a);
	} else {
		g_dbus_method_invocation_return_value(t->inv, g_variant_new("(bus)", FALSE, ms, "The server did not answer"));
	}
	g_free(t);
}

static void test_server(GDBusMethodInvocation *inv, const char *spec)
{
	g_autoptr(GError) error = NULL;
	Upstream *u = upstream_parse(spec, &error);
	Request *r;
	g_autoptr(GByteArray) q = NULL;
	TestCall *t;
	int rcode;

	if (!u) {
		g_dbus_method_invocation_return_value(inv, g_variant_new("(bus)", FALSE, 0, error->message));
		return;
	}
	t = g_new0(TestCall, 1);
	t->inv = inv;
	t->start = g_get_monotonic_time();
	r = request_new(CLIENT_INTERNAL);
	r->done = on_test_answer;
	r->done_data = t;
	r->no_cache = TRUE;
	q = dns_build_simple_query("example.com", DNS_TYPE_A, 0);
	dns_parse_query(q->data, q->len, &r->q, &rcode);
	r->ups = g_ptr_array_new_with_free_func((GDestroyNotify)upstream_unref);
	g_ptr_array_add(r->ups, u);
	r->deadline = g_timeout_add(DEADLINE_MS, on_deadline, r);
	request_next(r);
}

static void settings_changed(const char *property)
{
	save_settings();
	emit_changed(property, get_property(property));
}

static void on_method_call(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			   const char *method, GVariant *params, GDBusMethodInvocation *inv, gpointer user_data)
{
	g_autoptr(GError) error = NULL;

	touch_idle();
	if (!check_caller(sender, &error)) {
		g_dbus_method_invocation_return_gerror(inv, error);
		return;
	}
	if (!strcmp(method, "Pause")) {
		guint32 seconds;

		g_variant_get(params, "(u)", &seconds);
		self.paused_until = seconds ? now_s() + MIN(seconds, 7 * 86400) : 0;
		save_settings();
		cache_flush();
		apply_state();
		emit_changed("PausedUntil", get_property("PausedUntil"));
		g_dbus_method_invocation_return_value(inv, NULL);
	} else if (!strcmp(method, "GetCatalog")) {
		g_dbus_method_invocation_return_value(inv, g_variant_new("(@aa{sv})", catalog_variant()));
	} else if (!strcmp(method, "AddList")) {
		const char *url, *name;
		const CatalogEntry *c;
		List *l;
		g_autofree char *id = NULL;

		g_variant_get(params, "(&s&s)", &url, &name);
		c = catalog_find(url);
		if (c) {
			url = c->url;
			name = c->name;
			id = g_strdup(c->id);
		} else if (!g_str_has_prefix(url, "https://")) {
			g_dbus_method_invocation_return_error(inv, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS,
							      "Lists are secure web addresses (https://…)");
			return;
		}
		if ((l = list_find_url(url))) {
			l->enabled = TRUE;
		} else {
			if (!id)
				id = g_strdup_printf("custom-%08x", g_random_int());
			l = list_new(id, *name ? name : url, url, TRUE);
			g_ptr_array_add(self.lists, l);
		}
		save_lists();
		emit_changed("Lists", lists_variant());
		if (self.s.enabled)
			list_download(l);
		g_dbus_method_invocation_return_value(inv, g_variant_new("(s)", l->id));
	} else if (!strcmp(method, "RemoveList")) {
		const char *id;
		List *l;

		g_variant_get(params, "(&s)", &id);
		if (!(l = list_find(id))) {
			g_dbus_method_invocation_return_error(inv, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS, "No list %s", id);
			return;
		}
		{
			g_autofree char *bin = list_bin_path(l);

			g_unlink(bin);
		}
		g_ptr_array_remove(self.lists, l);
		save_lists();
		emit_changed("Lists", lists_variant());
		g_dbus_method_invocation_return_value(inv, NULL);
	} else if (!strcmp(method, "SetListEnabled")) {
		const char *id;
		gboolean on;
		List *l;

		g_variant_get(params, "(&sb)", &id, &on);
		if (!(l = list_find(id))) {
			g_dbus_method_invocation_return_error(inv, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS, "No list %s", id);
			return;
		}
		l->enabled = on;
		list_load_set(l);
		save_lists();
		emit_changed("Lists", lists_variant());
		if (on && !l->set && self.s.enabled)
			list_download(l);
		g_dbus_method_invocation_return_value(inv, NULL);
	} else if (!strcmp(method, "UpdateLists")) {
		update_lists(TRUE);
		g_dbus_method_invocation_return_value(inv, NULL);
	} else if (!strcmp(method, "GetRules")) {
		g_ptr_array_add(self.rules, NULL);
		g_dbus_method_invocation_return_value(inv, g_variant_new("(^as)", (char **)self.rules->pdata));
		g_ptr_array_set_size(self.rules, self.rules->len - 1);
	} else if (!strcmp(method, "AddRule")) {
		const char *input;
		g_autofree char *rule = NULL;

		g_variant_get(params, "(&s)", &input);
		rule = canonical_rule(input, &error);
		if (!rule) {
			g_dbus_method_invocation_return_gerror(inv, error);
			return;
		}
		/* Block and exception for the same domain: the newer one wins */
		{
			g_autofree char *opposite = g_str_has_prefix(rule, "@@") ? g_strdup(rule + 2)
										 : g_strconcat("@@", rule, NULL);

			g_ptr_array_remove_fast(self.rules, NULL);
			for (guint i = 0; i < self.rules->len; i++)
				if (!strcmp(g_ptr_array_index(self.rules, i), opposite))
					g_ptr_array_remove_index(self.rules, i--);
		}
		if (!has_rule(rule))
			g_ptr_array_add(self.rules, g_strdup(rule));
		rebuild_own();
		save_rules();
		cache_flush();
		emit_changed("RuleCount", get_property("RuleCount"));
		g_dbus_method_invocation_return_value(inv, g_variant_new("(s)", rule));
	} else if (!strcmp(method, "RemoveRule")) {
		const char *rule;
		gboolean found = FALSE;

		g_variant_get(params, "(&s)", &rule);
		for (guint i = 0; i < self.rules->len; i++)
			if (!strcmp(g_ptr_array_index(self.rules, i), rule)) {
				g_ptr_array_remove_index(self.rules, i--);
				found = TRUE;
			}
		if (found) {
			rebuild_own();
			save_rules();
			cache_flush();
			emit_changed("RuleCount", get_property("RuleCount"));
		}
		g_dbus_method_invocation_return_value(inv, NULL);
	} else if (!strcmp(method, "Check")) {
		const char *name;
		g_autofree char *ascii = NULL;
		g_autofree char *lower = NULL;
		Decision d;

		g_variant_get(params, "(&s)", &name);
		ascii = g_hostname_to_ascii(name);
		lower = g_ascii_strdown(ascii ? ascii : name, -1);
		/* As if the filter were on, so Settings can show it while it is off */
		{
			gboolean was = self.s.enabled;
			gint64 p = self.paused_until;

			self.s.enabled = TRUE;
			self.paused_until = 0;
			d = decide(lower, NULL);
			self.s.enabled = was;
			self.paused_until = p;
		}
		g_dbus_method_invocation_return_value(inv, g_variant_new("(us)", d.status, d.reason ? d.reason : ""));
		decision_clear(&d);
	} else if (!strcmp(method, "CheckNames")) {
		const char *app;
		g_autoptr(GVariantIter) names = NULL;
		const char *name;
		GVariantBuilder b;
		gboolean was = self.s.enabled;
		gint64 p = self.paused_until;
		guint n = 0;

		g_variant_get(params, "(&sas)", &app, &names);
		g_variant_builder_init(&b, G_VARIANT_TYPE("a(us)"));
		/* As Check: as if the filter were on */
		self.s.enabled = TRUE;
		self.paused_until = 0;
		while (g_variant_iter_next(names, "&s", &name) && n++ < 2000) {
			g_autofree char *ascii = g_hostname_to_ascii(name);
			g_autofree char *lower = g_ascii_strdown(ascii ? ascii : name, -1);
			Decision d = decide(lower, *app ? app : NULL);

			g_variant_builder_add(&b, "(us)", d.status, d.reason ? d.reason : "");
			decision_clear(&d);
		}
		self.s.enabled = was;
		self.paused_until = p;
		g_dbus_method_invocation_return_value(inv, g_variant_new("(a(us))", &b));
	} else if (!strcmp(method, "GetNamesOf") || !strcmp(method, "GetAskersOf")) {
		guint32 days;
		const char *arg;
		gboolean names = !strcmp(method, "GetNamesOf");

		g_variant_get(params, "(u&s)", &days, &arg);
		g_dbus_method_invocation_return_value(
			inv, g_variant_new("(@a(su))", get_pairs(CLAMP(days, 1, 90), names ? arg : NULL, names ? NULL : arg,
								 names ? 300 : 100)));
	} else if (!strcmp(method, "GetStats")) {
		guint32 days;

		g_variant_get(params, "(u)", &days);
		g_dbus_method_invocation_return_value(inv, g_variant_new("(@a{sv})", get_stats(CLAMP(days, 1, 90))));
	} else if (!strcmp(method, "GetLog")) {
		guint32 before, limit, filter;
		const char *search;

		g_variant_get(params, "(uu&su)", &before, &limit, &search, &filter);
		g_dbus_method_invocation_return_value(
			inv, g_variant_new("(@a" LOG_TYPE ")", get_log(before, MIN(limit, 2000), search, filter)));
	} else if (!strcmp(method, "GetLogNames")) {
		guint32 offset, limit, filter;
		const char *search;

		g_variant_get(params, "(uu&su)", &offset, &limit, &search, &filter);
		g_dbus_method_invocation_return_value(
			inv, g_variant_new("(@a(" LOG_TYPE "uu))", get_log_names(offset, MIN(limit, 2000), search, filter)));
	} else if (!strcmp(method, "ClearLog")) {
		clear_log();
		save_log();
		g_dbus_method_invocation_return_value(inv, NULL);
	} else if (!strcmp(method, "ClearStats")) {
		g_array_set_size(self.hours, 0);
		g_ptr_array_set_size(self.days, 0);
		save_stats();
		g_dbus_method_invocation_return_value(inv, NULL);
	} else if (!strcmp(method, "TestServer")) {
		const char *spec;

		g_variant_get(params, "(&s)", &spec);
		test_server(inv, spec);
	} else if (!strcmp(method, "Watch")) {
		if (!g_hash_table_contains(self.watchers, sender))
			g_hash_table_insert(self.watchers, g_strdup(sender),
					    GUINT_TO_POINTER(g_bus_watch_name_on_connection(
						    bus, sender, G_BUS_NAME_WATCHER_FLAGS_NONE, NULL, on_watcher_vanished,
						    NULL, NULL)));
		g_dbus_method_invocation_return_value(inv, NULL);
	} else if (!strcmp(method, "Unwatch")) {
		g_hash_table_remove(self.watchers, sender);
		touch_idle();
		g_dbus_method_invocation_return_value(inv, NULL);
	} else {
		g_dbus_method_invocation_return_error(inv, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_METHOD, "No method %s", method);
	}
}

static GVariant *on_get_property(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
				 const char *property, GError **error, gpointer user_data)
{
	touch_idle();
	if (private_property(property) && !check_caller(sender, error))
		return NULL;
	return get_property(property);
}

static gboolean on_set_property(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
				const char *property, GVariant *value, GError **error, gpointer user_data)
{
	Settings *s = &self.s;

	touch_idle();
	if (!check_caller(sender, error))
		return FALSE;
	if (!strcmp(property, "Enabled")) {
		s->enabled = g_variant_get_boolean(value);
		if (!s->enabled)
			self.paused_until = 0;
		settings_changed(property);
		apply_state();
		/* Turned on for the first time: the lists come right away */
		if (s->enabled)
			schedule_updates(1);
	} else if (!strcmp(property, "BlockMode")) {
		const char *mode = g_variant_get_string(value, NULL);

		if (strcmp(mode, "null-ip") && strcmp(mode, "nxdomain") && strcmp(mode, "refused")) {
			g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS, "Unknown mode %s", mode);
			return FALSE;
		}
		g_free(s->block_mode);
		s->block_mode = g_strdup(mode);
		settings_changed(property);
	} else if (!strcmp(property, "CustomServers")) {
		s->custom = g_variant_get_boolean(value);
		cache_flush();
		settings_changed(property);
		update_status();
	} else if (!strcmp(property, "Servers")) {
		g_auto(GStrv) servers = g_variant_dup_strv(value, NULL);

		for (guint i = 0; servers[i]; i++) {
			Upstream *u = upstream_parse(servers[i], error);

			if (!u)
				return FALSE;
			upstream_unref(u);
		}
		g_strfreev(s->servers);
		s->servers = g_steal_pointer(&servers);
		set_custom_servers();
		settings_changed(property);
		update_status();
	} else if (!strcmp(property, "LocalViaNetwork")) {
		s->local_via_network = g_variant_get_boolean(value);
		cache_flush();
		settings_changed(property);
	} else if (!strcmp(property, "SharedClients") || !strcmp(property, "RedirectDns") ||
		   !strcmp(property, "BlockEncryptedDns")) {
		gboolean v = g_variant_get_boolean(value);

		if (!strcmp(property, "SharedClients"))
			s->shared_clients = v;
		else if (!strcmp(property, "RedirectDns"))
			s->redirect_dns = v;
		else
			s->block_encrypted = v;
		settings_changed(property);
		if (s->enabled && self.listening && self.system)
			firewall_on();
	} else if (!strcmp(property, "KeepLog")) {
		s->keep_log = g_variant_get_boolean(value);
		settings_changed(property);
		save_log();
	} else if (!strcmp(property, "LogSize")) {
		s->log_size = CLAMP(g_variant_get_uint32(value), 100, 50000);
		log_resize(s->log_size);
		settings_changed(property);
	} else if (!strcmp(property, "StatsDays")) {
		s->stats_days = CLAMP(g_variant_get_uint32(value), 1, 90);
		settings_changed(property);
	} else if (!strcmp(property, "UpdateInterval")) {
		s->update_hours = MIN(g_variant_get_uint32(value), 24 * 30);
		settings_changed(property);
	} else if (!strcmp(property, "UpdateOnMobileData")) {
		s->update_on_mobile = g_variant_get_boolean(value);
		settings_changed(property);
	} else {
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_PROPERTY_READ_ONLY, "%s is read-only", property);
		return FALSE;
	}
	return TRUE;
}

static void on_bus_acquired(GDBusConnection *bus, const char *name, gpointer user_data)
{
	static const GDBusInterfaceVTable vtable = {on_method_call, on_get_property, on_set_property};
	GDBusNodeInfo *info = g_dbus_node_info_new_for_xml(introspection, NULL);

	self.bus = bus;
	g_dbus_connection_register_object(bus, OBJECT_PATH, info->interfaces[0], &vtable, NULL, NULL, NULL);
	g_dbus_node_info_unref(info);
}

static void on_name_lost(GDBusConnection *bus, const char *name, gpointer user_data)
{
	g_warning("Lost the bus name %s", name);
	g_main_loop_quit(self.loop);
}

static gboolean on_signal(gpointer user_data)
{
	g_main_loop_quit(self.loop);
	return G_SOURCE_REMOVE;
}

/* ===================================================================== main */

/* ExecStopPost and package removal: NetworkManager's DNS again, our firewall rules gone */
static int cleanup(void)
{
	self.system = TRUE;
	set_resolv_link(FALSE);
	firewall_off();
	return 0;
}

int main(int argc, char **argv)
{
	gboolean session = FALSE, no_system = FALSE, do_cleanup = FALSE;
	int port = 53;
	g_autofree char *state_dir = NULL;
	g_autofree char *link_dir = NULL;
	g_autoptr(GError) error = NULL;
	GOptionEntry entries[] = {
		{"session", 0, 0, G_OPTION_ARG_NONE, &session, "Use the session bus (testing)", NULL},
		{"no-system", 0, 0, G_OPTION_ARG_NONE, &no_system, "Leave resolv.conf and iptables alone (testing)", NULL},
		{"port", 0, 0, G_OPTION_ARG_INT, &port, "Port on 127.0.0.1 and ::1 (default 53)", "PORT"},
		{"state-dir", 0, 0, G_OPTION_ARG_FILENAME, &state_dir, "Settings, lists and statistics", "DIR"},
		{"link-dir", 0, 0, G_OPTION_ARG_FILENAME, &link_dir, "Where the resolv.conf link lives", "DIR"},
		{"ignore-portal", 0, 0, G_OPTION_ARG_NONE, &self.ignore_portal,
		 "Use the own servers even when NetworkManager sees a login page (testing)", NULL},
		{"cleanup", 0, 0, G_OPTION_ARG_NONE, &do_cleanup, "Undo resolv.conf link and firewall rules, then exit",
		 NULL},
		{NULL},
	};
	g_autoptr(GOptionContext) ctx = g_option_context_new("- DNS filter of the Plinius phone");
	g_autoptr(GResolver) resolver = NULL;

	g_option_context_add_main_entries(ctx, entries, NULL);
	if (!g_option_context_parse(ctx, &argc, &argv, &error)) {
		g_printerr("%s\n", error->message);
		return 2;
	}
	self.state_dir = g_strdup(state_dir ? state_dir : DEFAULT_STATE_DIR);
	self.link_dir = g_strdup(link_dir ? link_dir : DEFAULT_LINK_DIR);
	self.port = (guint16)CLAMP(port, 1, 65535);
	self.system = !no_system && !session;
	if (do_cleanup)
		return cleanup();

	g_mkdir_with_parents(self.state_dir, 0700);
	fix_state_modes(self.state_dir);
	/* Our own lookups (encrypted servers, list downloads) bypass the filter */
	resolver = g_object_new(bootstrap_resolver_get_type(), NULL);
	g_resolver_set_default(resolver);
	pending_queries = g_hash_table_new_full(NULL, NULL, NULL, (GDestroyNotify)g_byte_array_unref);

	self.loop = g_main_loop_new(NULL, FALSE);
	self.udp_fds = g_ptr_array_new();
	self.udp_sources = g_array_new(FALSE, FALSE, sizeof(guint));
	self.links = g_ptr_array_new_with_free_func((GDestroyNotify)netlink_free);
	self.custom = g_ptr_array_new_with_free_func((GDestroyNotify)upstream_unref);
	self.cache = g_hash_table_new_full(g_str_hash, g_str_equal, NULL, (GDestroyNotify)cache_entry_free);
	self.watchers = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, unwatch_name);
	self.tcp_peers = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
	self.log_batch = g_ptr_array_new_with_free_func((GDestroyNotify)g_variant_unref);
	load_settings();
	load_lists();
	load_rules();
	load_stats();
	load_log();
	set_custom_servers();

	self.system_bus = g_bus_get_sync(G_BUS_TYPE_SYSTEM, NULL, &error);
	if (!self.system_bus) {
		g_warning("No system bus (no network DNS): %s", error->message);
		g_clear_error(&error);
	} else {
		g_dbus_connection_signal_subscribe(self.system_bus, "org.freedesktop.NetworkManager",
						   "org.freedesktop.DBus.Properties", "PropertiesChanged", NULL, NULL,
						   G_DBUS_SIGNAL_FLAGS_NONE, on_nm_changed, NULL, NULL);
		g_dbus_connection_signal_subscribe(self.system_bus, "org.freedesktop.login1",
						   "org.freedesktop.login1.Manager", "UserNew", "/org/freedesktop/login1", NULL,
						   G_DBUS_SIGNAL_FLAGS_NONE, on_users_changed, NULL, NULL);
		g_dbus_connection_signal_subscribe(self.system_bus, "org.freedesktop.login1",
						   "org.freedesktop.login1.Manager", "UserRemoved", "/org/freedesktop/login1",
						   NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_users_changed, NULL, NULL);
		read_nm_dns();
		self.connectivity = nm_uint("Connectivity");
		self.metered = nm_uint("Metered");
	}
	{
		g_autoptr(GFile) f = g_file_new_for_path(NM_RESOLV);

		self.nm_monitor = g_file_monitor_file(f, G_FILE_MONITOR_NONE, NULL, NULL);
		if (self.nm_monitor)
			g_signal_connect(self.nm_monitor, "changed", G_CALLBACK(on_nm_resolv_changed), NULL);
	}

	g_bus_own_name(session ? G_BUS_TYPE_SESSION : G_BUS_TYPE_SYSTEM, BUS_NAME, G_BUS_NAME_OWNER_FLAGS_NONE,
		       on_bus_acquired, NULL, on_name_lost, NULL, NULL);
	g_unix_signal_add(SIGTERM, on_signal, NULL);
	g_unix_signal_add(SIGINT, on_signal, NULL);
	self.save_source = g_timeout_add_seconds(SAVE_INTERVAL_S, on_save_timer, NULL);

	apply_state();
	g_main_loop_run(self.loop);

	/* Leaving: the phone gets NetworkManager's DNS back before the port closes */
	set_resolv_link(FALSE);
	if (self.system)
		firewall_off();
	listeners_stop();
	appid_shutdown();
	if (self.stats_dirty)
		save_stats();
	if (self.s.keep_log)
		save_log();
	return 0;
}
