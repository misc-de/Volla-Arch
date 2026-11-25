// SPDX-License-Identifier: MIT
// plinius-kmsg: the kernel log into the journal, without the vendor drivers' status lines.
//
// journald does not read the kernel log itself here (ReadKMsg=no, device-volla-ansuz). This
// service reads /dev/kmsg instead and hands every record to the journal, except those containing
// one of the fixed strings in DROP_LIST, or in LOCAL_DROP_LIST for the owner's own. On 2026-11-25
// the vendor modules and the Android container wrote 34 000 lines an hour (display fences, fuel
// gauge and charger rounds, mminfra clocks, a uevent echo of netdagent and ueventd); journald spent
// about 17 s of CPU an hour on them and kept its 512 MB full.
//
// Never dropped: crit, alert and emerg. What is dropped is counted: with the first line after an
// hour (no timer of its own, so no wake-up) one notice says how many and which string the most.
// A record keeps what journald would keep: PRIORITY, SYSLOG_FACILITY, SYSLOG_IDENTIFIER ("kernel",
// or the writer's name for lines from user space such as the Android container's), SYSLOG_PID and
// the device of the record; besides the kernel's time stamp as KERNEL_USEC and its sequence number
// as KERNEL_SEQNUM. The journal files them under _TRANSPORT=journal: `journalctl -t kernel` finds
// the kernel's own lines, `journalctl -k` none. The full log stays in dmesg.
//
// The last sequence number handed over waits in STATE_FILE (tmpfs, RuntimeDirectoryPreserve=), so
// a restart of the service does not hand over the same lines again; after a boot it begins with
// the oldest line in the kernel's buffer, as journald does.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/uio.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>

#include <systemd/sd-journal.h>

#ifndef KMSG_PATH
#define KMSG_PATH "/dev/kmsg"
#endif
#ifndef DROP_LIST
#define DROP_LIST "/usr/lib/plinius-kmsg/drop.list"
#endif
#ifndef LOCAL_DROP_LIST
#define LOCAL_DROP_LIST "/etc/plinius-kmsg/drop.list"
#endif
#ifndef STATE_FILE
#define STATE_FILE "/run/plinius-kmsg/seqnum"
#endif
/* Seams for the tests */
#ifndef kmsg_read
#define kmsg_read read
#endif
#ifndef kmsg_poll
#define kmsg_poll poll
#endif
#ifndef journal_sendv
#define journal_sendv sd_journal_sendv
#endif
#ifndef coalesce_sleep
#define coalesce_sleep nanosleep
#endif

#define MAX_PATTERNS 256
#define MAX_PATTERN_LEN 255
#define SUMMARY_S 3600
/* After a wake-up the service waits so long and then reads all that came meanwhile: a line reaches
 * the journal up to 2 s late. Measured 2026-11-25 (always-on display, USB charging, 700 lines a
 * minute): about 0.5 ms of CPU per wake-up; one wake-up per line before, 39 batches a minute with
 * 0.5 s, 16 with 2 s, 8 with 5 s. */
#define COALESCE_MS 2000
/* A record: up to 1 kB of text, escaped up to four times, and its device lines */
#define RECORD_MAX 8192

static struct {
	char *text[MAX_PATTERNS];
	unsigned long hits[MAX_PATTERNS];
	int n;
} patterns;

static unsigned long dropped;
static int64_t window_start = -1;
static uint64_t last_seq;
static int have_seq;
static int saved_dirty;
static int state_fd = -1;
static volatile sig_atomic_t stopping;

static int64_t
now_s (void)
{
	struct timespec ts;

	clock_gettime (CLOCK_BOOTTIME, &ts);
	return ts.tv_sec;
}

/* One string per line; empty lines and lines starting with # do not count */
static void
load_patterns (const char *path)
{
	FILE *f = fopen (path, "re");
	char line[1024];

	if (!f)
		return;
	while (fgets (line, sizeof line, f)) {
		size_t len = strcspn (line, "\n");

		while (len > 0 && (line[len - 1] == ' ' || line[len - 1] == '\t' || line[len - 1] == '\r'))
			len--;
		line[len] = '\0';
		if (len == 0 || line[0] == '#')
			continue;
		if (len > MAX_PATTERN_LEN || patterns.n == MAX_PATTERNS) {
			fprintf (stderr, "%s: ignored (too long or too many): %s\n", path, line);
			continue;
		}
		patterns.text[patterns.n++] = strdup (line);
	}
	fclose (f);
}

static void
load_state (void)
{
	FILE *f = fopen (STATE_FILE, "re");

	if (!f)
		return;
	if (fscanf (f, "%" SCNu64, &last_seq) == 1)
		have_seq = 1;
	fclose (f);
}

/* After every burst, not after every line. One write of the same length each time to the file
 * kept open: on its tmpfs it is never seen half written. */
static void
save_state (void)
{
	char line[32];
	int len;

	if (!saved_dirty)
		return;
	if (state_fd < 0)
		state_fd = open (STATE_FILE, O_WRONLY | O_CREAT | O_CLOEXEC, 0600);
	if (state_fd < 0)
		return;
	len = snprintf (line, sizeof line, "%020" PRIu64 "\n", last_seq);
	if (pwrite (state_fd, line, len, 0) == len)
		saved_dirty = 0;
}

static void
on_stop (int sig)
{
	(void) sig;
	stopping = 1;
}

/* The kernel writes bytes outside printable ASCII as \xNN; journald writes them back, so do we */
static void
unescape (char *s)
{
	char *out = s;

	while (*s) {
		unsigned int v;

		if (s[0] == '\\' && s[1] == 'x' && sscanf (s + 2, "%2x", &v) == 1 && s[2] && s[3]) {
			*out++ = (char) v;
			s += 4;
		} else {
			*out++ = *s++;
		}
	}
	*out = '\0';
}

/* As journald: "name[pid]: text" or "name: text" from user space. Returns the text after it. */
static char *
parse_identifier (char *text, const char **ident, const char **pid)
{
	size_t n = strcspn (text, " \t:[");
	char *p = text + n;

	if (n == 0)
		return text;
	if (*p == '[') {
		char *end = strchr (p, ']');

		if (!end || end[1] != ':')
			return text;
		*p = '\0';
		*end = '\0';
		*pid = p + 1;
		p = end + 1;
	} else if (*p != ':') {
		return text;
	}
	*p++ = '\0';
	*ident = text;
	while (*p == ' ')
		p++;
	return p;
}

static void
send_notice (const char *message)
{
	char *fields[] = { NULL, "PRIORITY=5", "SYSLOG_IDENTIFIER=plinius-kmsg" };
	struct iovec iov[3];

	if (asprintf (&fields[0], "MESSAGE=%s", message) < 0)
		return;
	for (int k = 0; k < 3; k++)
		iov[k] = (struct iovec) { fields[k], strlen (fields[k]) };
	journal_sendv (iov, 3);
	free (fields[0]);
}

/* With the first line after the hour is over: how many lines went, and the string most of them had */
static void
maybe_summary (void)
{
	int64_t now = now_s ();
	char *message;
	int top = 0;

	if (window_start < 0)
		window_start = now;
	if (now - window_start < SUMMARY_S)
		return;
	if (dropped > 0) {
		for (int k = 1; k < patterns.n; k++)
			if (patterns.hits[k] > patterns.hits[top])
				top = k;
		if (asprintf (&message,
			      "%lu vendor status lines left out of the journal in the last hour, "
			      "%lu of them with \"%s\" (all of them in dmesg)",
			      dropped, patterns.hits[top], patterns.text[top]) >= 0) {
			send_notice (message);
			free (message);
		}
	}
	dropped = 0;
	memset (patterns.hits, 0, sizeof patterns.hits);
	window_start = now;
}

/* Returns the index of the string that drops the line, or -1 */
static int
dropping (int level, const char *text)
{
	if (level <= LOG_CRIT)
		return -1;
	for (int k = 0; k < patterns.n; k++)
		if (strstr (text, patterns.text[k]))
			return k;
	return -1;
}

#define MAX_FIELDS 10

typedef struct {
	char *text[MAX_FIELDS];
	int n;
} Fields;

static __attribute__ ((format (printf, 2, 3))) void
add_field (Fields *f, const char *format, ...)
{
	va_list args;

	if (f->n == MAX_FIELDS)
		return;
	va_start (args, format);
	if (vasprintf (&f->text[f->n], format, args) >= 0)
		f->n++;
	va_end (args);
}

/* "prio,seq,usec,flags[,...];text\n" and then " KEY=VALUE" lines of the device */
static void
handle_record (char *record)
{
	unsigned int prio;
	uint64_t seq, usec;
	char *text, *dict, *body;
	const char *ident = NULL, *pid = NULL;
	struct iovec iov[MAX_FIELDS];
	Fields f = { .n = 0 };
	int hit;

	if (sscanf (record, "%u,%" SCNu64 ",%" SCNu64 ",", &prio, &seq, &usec) != 3)
		return;
	text = strchr (record, ';');
	if (!text)
		return;
	text++;
	if (have_seq && seq <= last_seq)
		return;
	last_seq = seq;
	have_seq = 1;
	saved_dirty = 1;

	dict = strchr (text, '\n');
	if (dict)
		*dict++ = '\0';
	unescape (text);
	maybe_summary ();
	hit = dropping (prio & 7, text);
	if (hit >= 0) {
		patterns.hits[hit]++;
		dropped++;
		return;
	}

	body = (prio >> 3) == 0 ? text : parse_identifier (text, &ident, &pid);
	add_field (&f, "MESSAGE=%s", body);
	add_field (&f, "PRIORITY=%u", prio & 7);
	add_field (&f, "SYSLOG_FACILITY=%u", prio >> 3);
	add_field (&f, "SYSLOG_IDENTIFIER=%s", ident ? ident : "kernel");
	if (pid)
		add_field (&f, "SYSLOG_PID=%s", pid);
	add_field (&f, "KERNEL_USEC=%" PRIu64, usec);
	add_field (&f, "KERNEL_SEQNUM=%" PRIu64, seq);
	while (dict && *dict == ' ') {
		char *line = dict + 1, *end = strchr (line, '\n');

		if (end)
			*end = '\0';
		if (!strncmp (line, "SUBSYSTEM=", 10) || !strncmp (line, "DEVICE=", 7))
			add_field (&f, "KERNEL_%s", line);
		dict = end ? end + 1 : NULL;
	}
	for (int k = 0; k < f.n; k++)
		iov[k] = (struct iovec) { f.text[k], strlen (f.text[k]) };
	journal_sendv (iov, f.n);
	for (int k = 0; k < f.n; k++)
		free (f.text[k]);
}

int
main (void)
{
	static char record[RECORD_MAX];
	const struct timespec coalesce = { COALESCE_MS / 1000, COALESCE_MS % 1000 * 1000000L };
	struct sigaction stop = { .sa_handler = on_stop };
	struct pollfd pfd;
	int fd;

	load_patterns (DROP_LIST);
	load_patterns (LOCAL_DROP_LIST);
	load_state ();
	/* No SA_RESTART: a stop ends the poll and the pause, and the last burst is saved */
	sigaction (SIGTERM, &stop, NULL);
	sigaction (SIGINT, &stop, NULL);
	fd = open (KMSG_PATH, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
	if (fd < 0) {
		fprintf (stderr, "%s: %s\n", KMSG_PATH, strerror (errno));
		return 1;
	}
	pfd = (struct pollfd) { .fd = fd, .events = POLLIN };
	for (;;) {
		ssize_t n = kmsg_read (fd, record, sizeof record - 1);

		if (n > 0) {
			record[n] = '\0';
			handle_record (record);
			continue;
		}
		if (n == 0) /* only a test's file ends */
			break;
		if (errno == EPIPE) {
			/* The kernel's buffer ran over before the lines were read */
			send_notice ("kernel log: lines lost, the kernel's buffer ran over before they were read");
			continue;
		}
		if (errno == EINTR) {
			if (stopping)
				break;
			continue;
		}
		if (errno != EAGAIN) {
			fprintf (stderr, "%s: %s\n", KMSG_PATH, strerror (errno));
			save_state ();
			return 1;
		}
		save_state ();
		if (stopping)
			break;
		if (kmsg_poll (&pfd, 1, -1) < 0) {
			if (errno == EINTR)
				continue;
			break;
		}
		coalesce_sleep (&coalesce, NULL);
	}
	save_state ();
	return 0;
}
