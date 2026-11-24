// SPDX-License-Identifier: MIT
/* Test harness for audio-hal-bridge (tests/daemons-hw/test_dhw_audio*.py).
 *
 * The bridge's source is included (its main renamed real_main), with stand-ins for everything
 * outside it, so its decisions can be watched:
 *   - the Android audio HAL: a module, a device and its streams that print what they are asked
 *     (open/close, routing, mode, volume, parameters) and deliver scripted samples;
 *   - libpipewire (not linked): streams, the thread loop, context, core, registry and the bound
 *     proxies, driven by commands that emit the events PipeWire would (state, process, globals,
 *     infos, params, metadata);
 *   - ALSA's control API (not linked): the earpiece amplifier's enumerated control;
 *   - the jack's evdev ioctls (name, switches) from files next to the input device in the test
 *     directory, the GLib timeouts (fired by command), WebRTC's canceller (records the reference).
 * Every message of the bridge and of the stand-ins goes to stdout, in order, one line each.
 *
 * Usage: harness <command>...   (unit mode: commands run in order on a bridge of the harness)
 *        harness main <command>... (the commands run first, then the bridge's own main; the
 *        script in $FAKE_SCRIPT runs in the stand-in PipeWire thread, one command per line)
 * A failure is scripted with fail:<name>[*<times>],...  (see the fails() calls below). */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <linux/input.h>

#include <alsa/asoundlib.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <pipewire/extensions/metadata.h>
#include <pipewire/pipewire.h>
#include <spa/param/audio/format-utils.h>
#include <spa/param/profile.h>
#include <spa/param/props.h>
#include <spa/pod/builder.h>
#include <spa/pod/parser.h>
#include <spa/utils/json.h>
#include <spa/utils/ringbuffer.h>

#include <android-config.h>
#include <hardware/audio.h>
#include <hardware/hardware.h>

#include "aec.h"

static void say(const char *fmt, ...) G_GNUC_PRINTF(1, 2);
static int harness_fprintf(FILE *f, const char *fmt, ...) G_GNUC_PRINTF(2, 3);
static guint fake_timeout_add(guint ms, GSourceFunc fn, gpointer data);
static gboolean fake_source_remove(guint id);
static int fake_ioctl(int fd, unsigned long req, ...);
static int fake_clock_gettime(clockid_t clock, struct timespec *t);
static int fake_hw_get_module_by_class(const char *class_id, const char *inst, const struct hw_module_t **module);

#define fprintf harness_fprintf
#define g_timeout_add fake_timeout_add
#define g_source_remove fake_source_remove
#define ioctl fake_ioctl
#define clock_gettime fake_clock_gettime
#define hw_get_module_by_class fake_hw_get_module_by_class
#define main real_main
#include "audio-hal-bridge.c"
#undef main
#undef hw_get_module_by_class
#undef clock_gettime
#undef ioctl
#undef g_source_remove
#undef g_timeout_add
#undef fprintf

static void exec_cmd(const char *line);

/* ---- Output: one line per message, in order; "compact" folds repeated lines (xN) ---- */

static pthread_mutex_t say_lock = PTHREAD_MUTEX_INITIALIZER;
static bool compact;
static char last_line[1024];
static int last_count;

static void flush_line(void)
{
	if (last_count > 1)
		printf("%s x%d\n", last_line, last_count);
	else if (last_count)
		printf("%s\n", last_line);
	last_count = 0;
	fflush(stdout);
}

static void vsay(const char *fmt, va_list ap)
{
	char line[1024];
	vsnprintf(line, sizeof(line), fmt, ap);
	size_t n = strlen(line);
	while (n && line[n - 1] == '\n')
		line[--n] = 0;
	pthread_mutex_lock(&say_lock);
	if (compact && last_count && !strcmp(line, last_line)) {
		last_count++;
	} else {
		flush_line();
		snprintf(last_line, sizeof(last_line), "%s", line);
		last_count = 1;
		if (!compact)
			flush_line();
	}
	pthread_mutex_unlock(&say_lock);
}

static void say(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	vsay(fmt, ap);
	va_end(ap);
}

static int harness_fprintf(FILE *f, const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	vsay(fmt, ap);
	va_end(ap);
	return 0;
}

/* ---- Scripted failures: fail:<name>[*<times>],... ---- */

static pthread_mutex_t fail_lock = PTHREAD_MUTEX_INITIALIZER;
static struct {
	char name[96];
	int left; /* -1: always */
} fail_list[64];
static int n_fails;

static void add_fails(const char *spec)
{
	g_auto(GStrv) parts = g_strsplit(spec, ",", -1);
	pthread_mutex_lock(&fail_lock);
	for (int i = 0; parts[i] && n_fails < (int)G_N_ELEMENTS(fail_list); i++) {
		if (!*parts[i])
			continue;
		char *star = strrchr(parts[i], '*');
		fail_list[n_fails].left = star ? atoi(star + 1) : -1;
		if (star)
			*star = 0;
		snprintf(fail_list[n_fails++].name, sizeof(fail_list[0].name), "%s", parts[i]);
	}
	pthread_mutex_unlock(&fail_lock);
}

static bool fails(const char *fmt, ...) G_GNUC_PRINTF(1, 2);
static bool fails(const char *fmt, ...)
{
	char name[96];
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(name, sizeof(name), fmt, ap);
	va_end(ap);
	bool fail = false;
	pthread_mutex_lock(&fail_lock);
	for (int i = 0; i < n_fails && !fail; i++)
		if (!strcmp(fail_list[i].name, name) && fail_list[i].left != 0) {
			if (fail_list[i].left > 0)
				fail_list[i].left--;
			fail = true;
		}
	pthread_mutex_unlock(&fail_lock);
	return fail;
}

/* ---- Actions at events: at:<event>:<n>:<sync|async>:<command> ---- */

static pthread_mutex_t event_lock = PTHREAD_MUTEX_INITIALIZER;
static struct {
	char event[32];
	int n;
	bool async;
	char cmd[256];
} actions[64];
static int n_actions;
static struct {
	char event[32];
	int n;
} counts[32];
static int n_counts;

static void *async_cmd(void *data)
{
	exec_cmd(data);
	free(data);
	return NULL;
}

static void run_async(const char *cmd)
{
	pthread_t t;
	pthread_create(&t, NULL, async_cmd, strdup(cmd));
	pthread_detach(t);
}

/* Counts the event and runs what was asked for at this count: inline in the thread that
 * caused it, or in a thread of its own (for events in which the bridge holds a port lock it
 * releases only by waiting) */
static void event(const char *name)
{
	int n = 0;
	pthread_mutex_lock(&event_lock);
	int i;
	for (i = 0; i < n_counts && strcmp(counts[i].event, name); i++)
		;
	if (i == n_counts && n_counts < (int)G_N_ELEMENTS(counts))
		snprintf(counts[n_counts++].event, sizeof(counts[0].event), "%s", name);
	if (i < n_counts)
		n = ++counts[i].n;
	pthread_mutex_unlock(&event_lock);
	for (int a = 0; a < n_actions; a++)
		if (actions[a].n == n && !strcmp(actions[a].event, name)) {
			if (actions[a].async)
				run_async(actions[a].cmd);
			else
				exec_cmd(actions[a].cmd);
		}
}

/* ---- The Android audio HAL ---- */

static size_t out_buffer = 16384, in_buffer = 4096, fm_buffer = 4096;
static uint32_t out_latency = 20;
static int in_low = 100, in_high = 200, fm_value = 300;

struct fake_out {
	struct audio_stream_out s;
	audio_devices_t device;
};

struct fake_in {
	struct audio_stream_in s;
	audio_devices_t device;
	audio_channel_mask_t mask;
};

static size_t out_buffer_size(const struct audio_stream *s) { return out_buffer; }
static uint32_t out_get_latency(const struct audio_stream_out *s) { return out_latency; }

static int out_set_parameters(struct audio_stream *s, const char *kv)
{
	say("out %s", kv);
	return 0;
}

static int out_standby(struct audio_stream *s)
{
	say("out-standby");
	event("out-standby");
	return 0;
}

static ssize_t out_write(struct audio_stream_out *s, const void *buffer, size_t bytes)
{
	const int16_t *v = buffer;
	size_t n = bytes / 2;
	bool silent = true;
	for (size_t i = 0; i < n && silent; i++)
		silent = !v[i];
	if (silent)
		say("out-write silence");
	else
		say("out-write data %d..%d", v[0], v[n - 1]);
	event("out-write");
	return bytes;
}

static size_t in_buffer_size(const struct audio_stream *s)
{
	return ((const struct fake_in *)s)->device == AUDIO_DEVICE_IN_FM_TUNER ? fm_buffer : in_buffer;
}

static int in_set_parameters(struct audio_stream *s, const char *kv)
{
	say("in %s", kv);
	return 0;
}

static int in_standby(struct audio_stream *s)
{
	say("in-standby");
	event("in-standby");
	return 0;
}

static ssize_t in_read(struct audio_stream_in *s, void *buffer, size_t bytes)
{
	struct fake_in *in = (struct fake_in *)s;
	bool fm = in->device == AUDIO_DEVICE_IN_FM_TUNER;
	uint32_t channels = audio_channel_count_from_in_mask(in->mask);
	int16_t *v = buffer;
	for (size_t i = 0; i < bytes / 2; i++)
		v[i] = fm ? fm_value : i % channels ? in_high : in_low;
	ssize_t r = fails("read:%#x", in->device) ? -EIO : (ssize_t)bytes;
	event(fm ? "fm-read" : "in-read");
	return r;
}

static struct fake_out *new_out(audio_devices_t device)
{
	struct fake_out *o = calloc(1, sizeof(*o));
	o->device = device;
	o->s.common.get_buffer_size = out_buffer_size;
	o->s.common.set_parameters = out_set_parameters;
	o->s.common.standby = out_standby;
	o->s.get_latency = out_get_latency;
	o->s.write = out_write;
	return o;
}

static struct fake_in *new_in(audio_devices_t device, audio_channel_mask_t mask)
{
	struct fake_in *i = calloc(1, sizeof(*i));
	i->device = device;
	i->mask = mask;
	i->s.common.get_buffer_size = in_buffer_size;
	i->s.common.set_parameters = in_set_parameters;
	i->s.common.standby = in_standby;
	i->s.read = in_read;
	return i;
}

static int dev_init_check(const struct audio_hw_device *dev) { return fails("init_check") ? -ENODEV : 0; }

static int dev_set_voice_volume(struct audio_hw_device *dev, float v)
{
	say("voice-volume=%.3f", v);
	return fails("voice_volume") ? -EINVAL : 0;
}

static int dev_set_mode(struct audio_hw_device *dev, audio_mode_t mode)
{
	say("set-mode=%d", mode);
	return 0;
}

static int dev_set_mic_mute(struct audio_hw_device *dev, int state)
{
	say("mic-mute=%d", state);
	return 0;
}

static int dev_set_parameters(struct audio_hw_device *dev, const char *kv)
{
	say("hal %s", kv);
	return 0;
}

static int dev_open_output_stream(struct audio_hw_device *dev, audio_io_handle_t handle, audio_devices_t devices,
				  audio_output_flags_t flags, struct audio_config *config,
				  struct audio_stream_out **stream_out, const char *address)
{
	say("open-out %#x handle=%d flags=%#x rate=%u mask=%#x format=%#x", devices, handle, flags,
	    config->sample_rate, config->channel_mask, config->format);
	if (fails("open_out:%#x", devices))
		return -EINVAL;
	if (fails("open_out_null:%#x", devices)) {
		*stream_out = NULL;
		return 0;
	}
	if (fails("out_mismatch:%#x", devices))
		config->sample_rate = 44100;
	*stream_out = &new_out(devices)->s;
	return 0;
}

static void dev_close_output_stream(struct audio_hw_device *dev, struct audio_stream_out *s)
{
	say("close-out %#x", ((struct fake_out *)s)->device);
}

static int dev_open_input_stream(struct audio_hw_device *dev, audio_io_handle_t handle, audio_devices_t devices,
				 struct audio_config *config, struct audio_stream_in **stream_in,
				 audio_input_flags_t flags, const char *address, audio_source_t source)
{
	say("open-in %#x handle=%d mask=%#x rate=%u source=%d", devices, handle, config->channel_mask,
	    config->sample_rate, source);
	if (devices == AUDIO_DEVICE_IN_FM_TUNER)
		event("fm-open");
	if (fails("open_in:%#x:%#x", devices, config->channel_mask))
		return -EINVAL;
	if (fails("open_in_null:%#x:%#x", devices, config->channel_mask)) {
		*stream_in = NULL;
		return 0;
	}
	if (fails("in_mismatch:%#x:%#x", devices, config->channel_mask))
		config->sample_rate = 44100;
	*stream_in = &new_in(devices, config->channel_mask)->s;
	return 0;
}

static void dev_close_input_stream(struct audio_hw_device *dev, struct audio_stream_in *s)
{
	audio_devices_t device = ((struct fake_in *)s)->device;
	say("close-in %#x", device);
	event(device == AUDIO_DEVICE_IN_FM_TUNER ? "fm-close" : "in-close");
}

static int dev_close(struct hw_device_t *d)
{
	say("hal-close");
	return 0;
}

static struct audio_hw_device fake_dev = {
	.common = {.close = dev_close},
	.init_check = dev_init_check,
	.set_voice_volume = dev_set_voice_volume,
	.set_mode = dev_set_mode,
	.set_mic_mute = dev_set_mic_mute,
	.set_parameters = dev_set_parameters,
	.open_output_stream = dev_open_output_stream,
	.close_output_stream = dev_close_output_stream,
	.open_input_stream = dev_open_input_stream,
	.close_input_stream = dev_close_input_stream,
};

static int module_open(const struct hw_module_t *module, const char *id, struct hw_device_t **device)
{
	say("hal-open %s", id);
	if (fails("hal_open"))
		return -ENODEV;
	*device = fails("hal_open_null") ? NULL : &fake_dev.common;
	return 0;
}

static struct hw_module_methods_t module_methods = {.open = module_open};
static struct hw_module_t fake_module = {.id = "audio", .name = "fake primary", .methods = &module_methods};

static int fake_hw_get_module_by_class(const char *class_id, const char *inst, const struct hw_module_t **module)
{
	say("hw-module %s.%s", class_id, inst);
	if (fails("hw_module"))
		return -ENOENT;
	*module = fails("hw_module_null") ? NULL : &fake_module;
	return 0;
}

/* ---- WebRTC's canceller: records the reference it got, and marks the capture block with it ---- */

struct aec {
	int dummy;
};
static struct aec fake_aec;
static int16_t last_ref;

struct aec *aec_new(int rate, int render_channels, int capture_channels)
{
	say("aec-new rate=%d render=%d capture=%d", rate, render_channels, capture_channels);
	return fails("aec_new") ? NULL : &fake_aec;
}

void aec_free(struct aec *aec) { say("aec-free"); }
void aec_render(struct aec *aec, const int16_t *frame) { last_ref = frame[0]; }
void aec_capture(struct aec *aec, int16_t *frame, int delay_ms) { frame[0] = last_ref; }

/* ---- ALSA's control API: the amplifier switch aw87xxx_profile_switch_1 ---- */

struct _snd_ctl {
	int unused;
};
struct _snd_ctl_elem_id {
	snd_ctl_elem_iface_t iface;
	char name[64];
};
struct _snd_ctl_elem_info {
	struct _snd_ctl_elem_id id;
	bool item_set;
	unsigned item;
};
struct _snd_ctl_elem_value {
	struct _snd_ctl_elem_id id;
	unsigned value;
};

static char amp_items[8][32] = {"Off", "Music", "Receiver"};
static int n_amp_items = 3; /* < 0: no such control */
static unsigned amp_current = 1;

size_t snd_ctl_elem_id_sizeof(void) { return sizeof(struct _snd_ctl_elem_id); }
size_t snd_ctl_elem_info_sizeof(void) { return sizeof(struct _snd_ctl_elem_info); }
size_t snd_ctl_elem_value_sizeof(void) { return sizeof(struct _snd_ctl_elem_value); }

int snd_ctl_open(snd_ctl_t **ctl, const char *name, int mode)
{
	static struct _snd_ctl card;
	say("alsa-open %s", name);
	if (fails("alsa_open"))
		return -ENODEV;
	*ctl = &card;
	return 0;
}

int snd_ctl_close(snd_ctl_t *ctl) { return 0; }
void snd_ctl_elem_id_set_interface(snd_ctl_elem_id_t *obj, snd_ctl_elem_iface_t val) { obj->iface = val; }
void snd_ctl_elem_id_set_name(snd_ctl_elem_id_t *obj, const char *val) { snprintf(obj->name, sizeof(obj->name), "%s", val); }
void snd_ctl_elem_info_set_id(snd_ctl_elem_info_t *obj, const snd_ctl_elem_id_t *ptr) { obj->id = *ptr; }

void snd_ctl_elem_info_set_item(snd_ctl_elem_info_t *obj, unsigned int val)
{
	obj->item_set = true;
	obj->item = val;
}

int snd_ctl_elem_info(snd_ctl_t *ctl, snd_ctl_elem_info_t *info)
{
	if (n_amp_items < 0 || info->id.iface != SND_CTL_ELEM_IFACE_MIXER || strcmp(info->id.name, "aw87xxx_profile_switch_1"))
		return -ENOENT;
	if (info->item_set ? fails("alsa_item:%u", info->item) : fails("alsa_info"))
		return -EIO;
	return 0;
}

unsigned int snd_ctl_elem_info_get_items(const snd_ctl_elem_info_t *obj) { return n_amp_items; }

const char *snd_ctl_elem_info_get_item_name(const snd_ctl_elem_info_t *obj)
{
	return obj->item < (unsigned)n_amp_items ? amp_items[obj->item] : "";
}

void snd_ctl_elem_value_set_id(snd_ctl_elem_value_t *obj, const snd_ctl_elem_id_t *ptr) { obj->id = *ptr; }

int snd_ctl_elem_read(snd_ctl_t *ctl, snd_ctl_elem_value_t *data)
{
	if (fails("alsa_read"))
		return -EIO;
	data->value = amp_current;
	return 0;
}

unsigned int snd_ctl_elem_value_get_enumerated(const snd_ctl_elem_value_t *obj, unsigned int idx) { return obj->value; }
void snd_ctl_elem_value_set_enumerated(snd_ctl_elem_value_t *obj, unsigned int idx, unsigned int val) { obj->value = val; }

int snd_ctl_elem_write(snd_ctl_t *ctl, snd_ctl_elem_value_t *data)
{
	if (fails("alsa_write"))
		return -EIO;
	amp_current = data->value;
	say("amp=%s", amp_items[amp_current]);
	return 0;
}

/* ---- The jack's input device: <device>.name holds its name, <device>.sw one line per state
 * read (EVIOCGSW), the switches that are on, comma separated (- for none) ---- */

static int sw_reads;

static int fake_ioctl(int fd, unsigned long req, ...)
{
	va_list ap;
	va_start(ap, req);
	void *arg = va_arg(ap, void *);
	va_end(ap);
	char link[64], path[PATH_MAX], file[PATH_MAX + 8];
	snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
	ssize_t len = readlink(link, path, sizeof(path) - 1);
	if (len < 0) {
		errno = EBADF;
		return -1;
	}
	path[len] = 0;
	unsigned size = _IOC_SIZE(req);
	gchar *text = NULL;
	if (_IOC_TYPE(req) == 'E' && _IOC_NR(req) == _IOC_NR(EVIOCGNAME(0))) {
		snprintf(file, sizeof(file), "%s.name", path);
		if (!g_file_get_contents(file, &text, NULL, NULL)) {
			errno = ENOTTY;
			return -1;
		}
		g_strchomp(text);
		snprintf(arg, size, "%s", text);
		int n = strlen(arg) + 1;
		g_free(text);
		return n;
	}
	if (_IOC_TYPE(req) == 'E' && _IOC_NR(req) == _IOC_NR(EVIOCGSW(0))) {
		snprintf(file, sizeof(file), "%s.sw", path);
		memset(arg, 0, size);
		if (g_file_get_contents(file, &text, NULL, NULL) && *g_strchomp(text)) {
			g_auto(GStrv) lines = g_strsplit(text, "\n", -1);
			int n = g_strv_length(lines);
			int at = sw_reads < n ? sw_reads : n - 1;
			sw_reads++;
			g_auto(GStrv) bits = g_strsplit(lines[at], ",", -1);
			for (int i = 0; bits[i]; i++)
				if (g_ascii_isdigit(*bits[i])) { /* "-": none */
					unsigned bit = atoi(bits[i]);
					((unsigned long *)arg)[bit / (8 * sizeof(long))] |= 1ul << (bit % (8 * sizeof(long)));
				}
		}
		g_free(text);
		return size;
	}
	errno = ENOTTY;
	return -1;
}

/* deadline(): CLOCK_REALTIME's nanoseconds can be fixed, to test the carry */
static long clock_nsec = -1;

static int fake_clock_gettime(clockid_t clock, struct timespec *t)
{
	int r = clock_gettime(clock, t);
	if (clock == CLOCK_REALTIME && clock_nsec >= 0)
		t->tv_nsec = clock_nsec;
	return r;
}

/* ---- GLib timeouts: recorded, fired by command ---- */

static struct {
	guint id, ms;
	GSourceFunc fn;
	gpointer data;
	bool pending;
} timeouts[64];
static int n_timeouts;
#define TIMEOUT_ID0 1000000u

static guint fake_timeout_add(guint ms, GSourceFunc fn, gpointer data)
{
	say("timeout-add %u", ms);
	int i = n_timeouts++ % (int)G_N_ELEMENTS(timeouts);
	timeouts[i].id = TIMEOUT_ID0 + n_timeouts;
	timeouts[i].ms = ms;
	timeouts[i].fn = fn;
	timeouts[i].data = data;
	timeouts[i].pending = true;
	return timeouts[i].id;
}

static gboolean fake_source_remove(guint id)
{
	if (id < TIMEOUT_ID0)
		return g_source_remove(id);
	for (size_t i = 0; i < G_N_ELEMENTS(timeouts); i++)
		if (timeouts[i].id == id && timeouts[i].pending) {
			timeouts[i].pending = false;
			say("timeout-remove %u", timeouts[i].ms);
			return TRUE;
		}
	say("timeout-remove of an unknown source %u", id);
	return FALSE;
}

static gboolean fire_timeout(gpointer data)
{
	guint ms = GPOINTER_TO_UINT(data);
	for (size_t i = 0; i < G_N_ELEMENTS(timeouts); i++)
		if (timeouts[i].pending && timeouts[i].ms == ms) {
			timeouts[i].pending = false;
			say("timeout-fire %u", ms);
			timeouts[i].fn(timeouts[i].data);
			return G_SOURCE_REMOVE;
		}
	say("no timeout %u pending", ms);
	return G_SOURCE_REMOVE;
}

/* ---- libpipewire ---- */

static struct bridge *B; /* the bridge the commands act on */
static bool main_mode;
static pthread_t script_thread;
static bool script_started;
static struct pw_thread_loop *started_loop;

enum spa_log_level pw_log_level = SPA_LOG_LEVEL_INFO;
struct spa_log_topic *const PW_LOG_TOPIC_DEFAULT = NULL;

void pw_log_logt(enum spa_log_level level, const struct spa_log_topic *topic, const char *file, int line,
		 const char *func, const char *fmt, ...)
{
	char msg[512];
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(msg, sizeof(msg), fmt, ap);
	va_end(ap);
	say("log %s", msg);
}

void pw_init(int *argc, char **argv[]) { say("pw-init"); }
void pw_deinit(void) { say("pw-deinit"); }

struct pw_thread_loop {
	pthread_mutex_t lock;
};
static struct pw_loop fake_loop;

static void *script_main(void *data);

struct pw_thread_loop *pw_thread_loop_new(const char *name, const struct spa_dict *props)
{
	struct pw_thread_loop *l = calloc(1, sizeof(*l));
	pthread_mutexattr_t attr;
	pthread_mutexattr_init(&attr);
	pthread_mutexattr_settype(&attr, PTHREAD_MUTEX_RECURSIVE);
	pthread_mutex_init(&l->lock, &attr);
	return l;
}

int pw_thread_loop_start(struct pw_thread_loop *loop)
{
	say("loop-start");
	if (fails("loop_start"))
		return -EIO;
	if (main_mode && getenv("FAKE_SCRIPT")) {
		script_started = true;
		started_loop = loop;
		pthread_create(&script_thread, NULL, script_main, getenv("FAKE_SCRIPT"));
	}
	return 0;
}

void pw_thread_loop_stop(struct pw_thread_loop *loop)
{
	say("loop-stop");
	if (script_started)
		pthread_join(script_thread, NULL);
	script_started = false;
}

void pw_thread_loop_destroy(struct pw_thread_loop *loop) { say("loop-destroy"); }
void pw_thread_loop_lock(struct pw_thread_loop *loop) { pthread_mutex_lock(&loop->lock); }
void pw_thread_loop_unlock(struct pw_thread_loop *loop) { pthread_mutex_unlock(&loop->lock); }
struct pw_loop *pw_thread_loop_get_loop(struct pw_thread_loop *loop) { return &fake_loop; }

/* Properties: a dictionary of copies */
#define PROPS_MAX 32
struct pw_properties *pw_properties_new(const char *key, ...)
{
	struct pw_properties *p = calloc(1, sizeof(*p));
	p->dict.items = calloc(PROPS_MAX, sizeof(struct spa_dict_item));
	va_list ap;
	va_start(ap, key);
	while (key) {
		const char *value = va_arg(ap, const char *);
		pw_properties_set(p, key, value);
		key = va_arg(ap, const char *);
	}
	va_end(ap);
	return p;
}

int pw_properties_set(struct pw_properties *p, const char *key, const char *value)
{
	struct spa_dict_item *items = (struct spa_dict_item *)p->dict.items;
	for (uint32_t i = 0; i < p->dict.n_items; i++)
		if (!strcmp(items[i].key, key)) {
			items[i].value = strdup(value);
			return 1;
		}
	if (p->dict.n_items < PROPS_MAX) {
		items[p->dict.n_items].key = strdup(key);
		items[p->dict.n_items++].value = strdup(value);
	}
	return 1;
}

void pw_properties_free(struct pw_properties *p) {}

static char *props_text(const struct spa_dict *dict)
{
	GString *s = g_string_new(NULL);
	for (uint32_t i = 0; dict && i < dict->n_items; i++)
		g_string_append_printf(s, "%s%s=%s", i ? " " : "", dict->items[i].key, dict->items[i].value);
	return g_string_free(s, FALSE);
}

/* Streams */
struct pw_stream {
	char name[128], media[128];
	struct pw_properties *props;
	const struct pw_stream_events *events;
	void *data;
	uint32_t node_id;
	enum pw_direction dir;
	bool destroyed;
	bool driving, no_done, no_buffer, no_data;
	int16_t value;
	uint32_t quantum, maxsize, offset;
	uint64_t requested;
	unsigned triggers;
	struct pw_buffer pwb;
	struct spa_buffer sb;
	struct spa_data sd;
	struct spa_chunk ch;
	int16_t mem[CHUNK_MAX];
};

static pthread_mutex_t streams_lock = PTHREAD_MUTEX_INITIALIZER;
static struct pw_stream *all_streams[128];
static int n_streams;
static uint32_t next_node_id = 200;

static struct pw_stream *stream_by_name(const char *name)
{
	struct pw_stream *s = NULL;
	pthread_mutex_lock(&streams_lock);
	for (int i = n_streams - 1; i >= 0 && !s; i--)
		if (!all_streams[i]->destroyed && !strcmp(all_streams[i]->name, name))
			s = all_streams[i];
	pthread_mutex_unlock(&streams_lock);
	if (!s)
		say("no stream %s", name);
	return s;
}

struct pw_stream *pw_stream_new_simple(struct pw_loop *loop, const char *name, struct pw_properties *props,
				       const struct pw_stream_events *events, void *data)
{
	const char *node = spa_dict_lookup(&props->dict, PW_KEY_NODE_NAME);
	g_autofree char *text = props_text(&props->dict);
	say("stream-new %s media=%s: %s", node, name, text);
	if (events == &stream_events && !B)
		B = ((struct port *)data)->bridge;
	if (fails("stream_new:%s", node)) {
		pw_properties_free(props);
		return NULL;
	}
	struct pw_stream *s = calloc(1, sizeof(*s));
	snprintf(s->name, sizeof(s->name), "%s", node);
	snprintf(s->media, sizeof(s->media), "%s", name);
	s->props = props;
	s->events = events;
	s->data = data;
	s->driving = true;
	s->value = 1000;
	s->quantum = 4096;
	s->maxsize = sizeof(s->mem);
	pthread_mutex_lock(&streams_lock);
	s->node_id = next_node_id++;
	if (n_streams < (int)G_N_ELEMENTS(all_streams))
		all_streams[n_streams++] = s;
	pthread_mutex_unlock(&streams_lock);
	return s;
}

static const char *position_name(uint32_t p)
{
	return p == SPA_AUDIO_CHANNEL_FL ? "FL" : p == SPA_AUDIO_CHANNEL_FR ? "FR" : p == SPA_AUDIO_CHANNEL_MONO ? "MONO" : "?";
}

int pw_stream_connect(struct pw_stream *s, enum pw_direction direction, uint32_t target_id, enum pw_stream_flags flags,
		      const struct spa_pod **params, uint32_t n_params)
{
	struct spa_audio_info_raw info = {0};
	char format[96] = "?";
	if (n_params == 1 && spa_format_audio_raw_parse(params[0], &info) >= 0) {
		int len = snprintf(format, sizeof(format), "%s/%u/%u/", info.format == SPA_AUDIO_FORMAT_S16_LE ? "S16LE" : "?",
				   info.rate, info.channels);
		for (uint32_t i = 0; i < info.channels && i < 2; i++)
			len += snprintf(format + len, sizeof(format) - len, "%s%s", i ? "," : "", position_name(info.position[i]));
	}
	s->dir = direction;
	say("stream-connect %s %s target=%d flags=%s%s%s format=%s", s->name,
	    direction == PW_DIRECTION_INPUT ? "input" : "output", (int)target_id,
	    flags & PW_STREAM_FLAG_AUTOCONNECT ? "autoconnect," : "", flags & PW_STREAM_FLAG_MAP_BUFFERS ? "map-buffers," : "",
	    flags & PW_STREAM_FLAG_DRIVER ? "driver" : "", format);
	return fails("stream_connect:%s", s->name) ? -EIO : 0;
}

void pw_stream_destroy(struct pw_stream *s)
{
	say("stream-destroy %s", s ? s->name : "(null)");
	if (s)
		s->destroyed = true;
}

const char *pw_stream_state_as_string(enum pw_stream_state state)
{
	switch (state) {
	case PW_STREAM_STATE_ERROR: return "error";
	case PW_STREAM_STATE_UNCONNECTED: return "unconnected";
	case PW_STREAM_STATE_CONNECTING: return "connecting";
	case PW_STREAM_STATE_PAUSED: return "paused";
	case PW_STREAM_STATE_STREAMING: return "streaming";
	}
	return "?";
}

uint32_t pw_stream_get_node_id(struct pw_stream *s) { return s ? s->node_id : SPA_ID_INVALID; }
bool pw_stream_is_driving(struct pw_stream *s) { return s->driving; }

/* A graph cycle: the stream's process, then trigger_done (unless that was switched off) */
int pw_stream_trigger_process(struct pw_stream *s)
{
	s->triggers++;
	s->events->process(s->data);
	if (!s->no_done)
		s->events->trigger_done(s->data);
	return 0;
}

/* Sinks get "the app's" samples (value), sources a buffer full of 0x5a5a to write into */
struct pw_buffer *pw_stream_dequeue_buffer(struct pw_stream *s)
{
	if (s->no_buffer)
		return NULL;
	s->sb.datas = &s->sd;
	s->sb.n_datas = 1;
	s->pwb.buffer = &s->sb;
	s->pwb.requested = s->requested;
	s->sd.chunk = &s->ch;
	s->sd.maxsize = s->maxsize;
	s->sd.data = s->no_data ? NULL : s->mem;
	if (s->dir == PW_DIRECTION_INPUT) {
		for (uint32_t i = 0; i < s->quantum / 2; i++)
			s->mem[(s->offset / 2 + i) % CHUNK_MAX] = s->value;
		s->ch.offset = s->offset;
		s->ch.size = s->quantum;
	} else {
		memset(s->mem, 0x5a, sizeof(s->mem));
		s->ch.offset = 0x7777;
		s->ch.size = 0x7777;
		s->ch.stride = 0;
	}
	return &s->pwb;
}

int pw_stream_queue_buffer(struct pw_stream *s, struct pw_buffer *b)
{
	if (s->dir != PW_DIRECTION_OUTPUT)
		return 0;
	if (!s->sd.data) {
		say("deliver %s no data", s->name);
		return 0;
	}
	uint32_t n = s->ch.size / 2;
	say("deliver %s %u offset=%u first=%d last=%d stride=%d", s->name, s->ch.size, s->ch.offset, n ? s->mem[0] : 0,
	    n ? s->mem[n - 1] : 0, s->ch.stride);
	return 0;
}

int pw_stream_set_control(struct pw_stream *s, uint32_t id, uint32_t n_values, float *values, ...)
{
	GString *text = g_string_new(NULL);
	va_list ap;
	va_start(ap, values);
	while (id) {
		g_string_append_printf(text, " %s=", id == SPA_PROP_volume ? "volume" : id == SPA_PROP_channelVolumes ? "channelVolumes" : "?");
		for (uint32_t i = 0; i < n_values; i++)
			g_string_append_printf(text, "%s%.3f", i ? "," : "", values[i]);
		id = va_arg(ap, uint32_t);
		if (!id)
			break;
		n_values = va_arg(ap, uint32_t);
		values = va_arg(ap, float *);
	}
	va_end(ap);
	say("control %s%s", s->name, text->str);
	g_string_free(text, TRUE);
	return 0;
}

/* Context, core, registry and the proxies bound from it: one kind of object */
struct pw_proxy {
	struct spa_interface iface;
	struct spa_hook_list hooks;
	uint32_t id;
	char type[64];
	bool destroyed;
};
struct pw_context {
	int unused;
};

static struct pw_proxy *objects[512];
static int n_objects;
static struct pw_proxy *the_registry;

static const char *short_type(const char *type)
{
	const char *c = strrchr(type, ':');
	return c ? c + 1 : type;
}

static const char *param_name(uint32_t id)
{
	return id == SPA_PARAM_EnumProfile ? "EnumProfile" : id == SPA_PARAM_Profile ? "Profile"
	       : id == SPA_PARAM_Props ? "Props" : "?";
}

static struct pw_proxy *new_object(const char *type, uint32_t id);

static int reg_add_listener(void *o, struct spa_hook *l, const struct pw_registry_events *e, void *data)
{
	B = data;
	spa_hook_list_append(&((struct pw_proxy *)o)->hooks, l, e, data);
	return 0;
}

static void *reg_bind(void *o, uint32_t id, const char *type, uint32_t version, size_t size)
{
	say("bind %u %s", id, short_type(type));
	return fails("bind:%u", id) ? NULL : new_object(type, id);
}

static int dev_add_listener(void *o, struct spa_hook *l, const struct pw_device_events *e, void *data)
{
	spa_hook_list_append(&((struct pw_proxy *)o)->hooks, l, e, data);
	return 0;
}

static int node_add_listener(void *o, struct spa_hook *l, const struct pw_node_events *e, void *data)
{
	spa_hook_list_append(&((struct pw_proxy *)o)->hooks, l, e, data);
	return 0;
}

static int md_add_listener(void *o, struct spa_hook *l, const struct pw_metadata_events *e, void *data)
{
	spa_hook_list_append(&((struct pw_proxy *)o)->hooks, l, e, data);
	return 0;
}

static int obj_subscribe(void *o, uint32_t *ids, uint32_t n)
{
	GString *s = g_string_new(NULL);
	for (uint32_t i = 0; i < n; i++)
		g_string_append_printf(s, "%s%s", i ? "," : "", param_name(ids[i]));
	say("subscribe %u %s", ((struct pw_proxy *)o)->id, s->str);
	g_string_free(s, TRUE);
	return 0;
}

static int dev_enum(void *o, int seq, uint32_t id, uint32_t start, uint32_t num, const struct spa_pod *filter)
{
	say("enum-params %u %s", ((struct pw_proxy *)o)->id, param_name(id));
	return 0;
}

static int dev_set_param(void *o, uint32_t id, uint32_t flags, const struct spa_pod *param)
{
	int32_t index = -1;
	bool save = true;
	spa_pod_parse_object(param, SPA_TYPE_OBJECT_ParamProfile, NULL, SPA_PARAM_PROFILE_index, SPA_POD_Int(&index),
			     SPA_PARAM_PROFILE_save, SPA_POD_Bool(&save));
	say("set-profile %u %s index=%d save=%d", ((struct pw_proxy *)o)->id, param_name(id), index, save);
	return 0;
}

static struct pw_registry *core_get_registry(void *o, uint32_t version, size_t size)
{
	say("get-registry");
	if (fails("registry"))
		return NULL;
	the_registry = new_object(PW_TYPE_INTERFACE_Registry, 0);
	return (struct pw_registry *)the_registry;
}

static const struct pw_core_methods core_methods = {PW_VERSION_CORE_METHODS, .get_registry = core_get_registry};
static const struct pw_registry_methods registry_methods = {PW_VERSION_REGISTRY_METHODS, .add_listener = reg_add_listener,
							    .bind = reg_bind};
static const struct pw_device_methods device_methods = {PW_VERSION_DEVICE_METHODS, .add_listener = dev_add_listener,
							.subscribe_params = obj_subscribe, .enum_params = dev_enum,
							.set_param = dev_set_param};
static const struct pw_node_methods node_methods = {PW_VERSION_NODE_METHODS, .add_listener = node_add_listener,
						    .subscribe_params = obj_subscribe};
static const struct pw_metadata_methods metadata_methods = {PW_VERSION_METADATA_METHODS, .add_listener = md_add_listener};

static struct pw_proxy *new_object(const char *type, uint32_t id)
{
	struct pw_proxy *o = calloc(1, sizeof(*o));
	const void *methods = !strcmp(type, PW_TYPE_INTERFACE_Core) ? (const void *)&core_methods
			      : !strcmp(type, PW_TYPE_INTERFACE_Registry) ? (const void *)&registry_methods
			      : !strcmp(type, PW_TYPE_INTERFACE_Device) ? (const void *)&device_methods
			      : !strcmp(type, PW_TYPE_INTERFACE_Node) ? (const void *)&node_methods
			      : (const void *)&metadata_methods;
	o->iface = SPA_INTERFACE_INIT(type, 0, methods, o);
	spa_hook_list_init(&o->hooks);
	o->id = id;
	snprintf(o->type, sizeof(o->type), "%s", type);
	if (n_objects < (int)G_N_ELEMENTS(objects))
		objects[n_objects++] = o;
	return o;
}

static struct pw_proxy *object_by_id(uint32_t id)
{
	for (int i = n_objects - 1; i >= 0; i--)
		if (!objects[i]->destroyed && objects[i]->id == id && strcmp(objects[i]->type, PW_TYPE_INTERFACE_Registry) &&
		    strcmp(objects[i]->type, PW_TYPE_INTERFACE_Core))
			return objects[i];
	say("no proxy %u", id);
	return NULL;
}

struct pw_context *pw_context_new(struct pw_loop *loop, struct pw_properties *props, size_t size)
{
	say("context-new");
	return fails("context") ? NULL : calloc(1, sizeof(struct pw_context));
}

struct pw_core *pw_context_connect(struct pw_context *context, struct pw_properties *props, size_t size)
{
	say("context-connect");
	return fails("core") ? NULL : (struct pw_core *)new_object(PW_TYPE_INTERFACE_Core, 0);
}

int pw_core_disconnect(struct pw_core *core)
{
	say("core-disconnect");
	return 0;
}

void pw_context_destroy(struct pw_context *context) { say("context-destroy"); }

void pw_proxy_destroy(struct pw_proxy *proxy)
{
	say("proxy-destroy %u %s", proxy->id, short_type(proxy->type));
	proxy->destroyed = true;
}

uint32_t pw_proxy_get_bound_id(struct pw_proxy *proxy) { return proxy->id; }

/* ---- D-Bus client of the main mode's script ---- */

static GDBusConnection *client;
static GMainContext *client_context;
static int n_signals;
static GMutex barrier_lock;
static GCond barrier_cond;
static bool barrier_done;

static gboolean barrier_reached(gpointer data)
{
	g_mutex_lock(&barrier_lock);
	barrier_done = true;
	g_cond_signal(&barrier_cond);
	g_mutex_unlock(&barrier_lock);
	return G_SOURCE_REMOVE;
}

/* Waits until the main loop has run everything queued before (idles queue at a higher priority) */
static void barrier(void)
{
	g_mutex_lock(&barrier_lock);
	barrier_done = false;
	g_idle_add_full(G_PRIORITY_LOW, barrier_reached, NULL, NULL);
	while (!barrier_done)
		g_cond_wait(&barrier_cond, &barrier_lock);
	g_mutex_unlock(&barrier_lock);
}

static void on_client_signal(GDBusConnection *c, const char *sender, const char *path, const char *iface,
			     const char *signal, GVariant *params, gpointer data)
{
	g_autofree char *text = g_variant_print(params, FALSE);
	say("signal %s.%s %s %s", iface, signal, path, text);
	n_signals++;
}

static GVariant *client_call(const char *iface, const char *method, GVariant *args, GError **error)
{
	return g_dbus_connection_call_sync(client, "org.mobian_project.CallAudio", "/org/mobian_project/CallAudio", iface,
					   method, args, NULL, G_DBUS_CALL_FLAGS_NONE, 5000, NULL, error);
}

static void say_reply(const char *what, GVariant *reply, GError *error)
{
	if (reply) {
		g_autofree char *text = g_variant_print(reply, FALSE);
		say("reply %s %s", what, text);
		g_variant_unref(reply);
	} else {
		say("error %s %s", what, error->message);
		g_error_free(error);
	}
}

static void client_connect(void)
{
	GError *error = NULL;
	client_context = g_main_context_new();
	g_main_context_push_thread_default(client_context);
	client = g_dbus_connection_new_for_address_sync(getenv("DBUS_SESSION_BUS_ADDRESS"),
						       G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT |
							       G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
						       NULL, NULL, &error);
	if (!client) {
		say("no session bus: %s", error->message);
		exit(3);
	}
	/* Until the bridge has its name */
	for (int i = 0; i < 1000; i++) {
		g_autoptr(GVariant) r = g_dbus_connection_call_sync(client, "org.freedesktop.DBus", "/org/freedesktop/DBus",
								   "org.freedesktop.DBus", "NameHasOwner",
								   g_variant_new("(s)", "org.mobian_project.CallAudio"),
								   G_VARIANT_TYPE("(b)"), 0, 5000, NULL, NULL);
		gboolean owned = FALSE;
		if (r)
			g_variant_get(r, "(b)", &owned);
		if (owned)
			break;
		g_usleep(10000);
	}
	g_dbus_connection_signal_subscribe(client, NULL, "org.freedesktop.DBus.Properties", "PropertiesChanged",
					   "/org/mobian_project/CallAudio", NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_client_signal,
					   NULL, NULL);
	/* The match rule is in place once a call after it returned */
	g_autoptr(GVariant) r = g_dbus_connection_call_sync(client, "org.freedesktop.DBus", "/org/freedesktop/DBus",
							   "org.freedesktop.DBus", "GetId", NULL, NULL, 0, 5000, NULL, NULL);
	say("dbus ready");
}

static void *script_main(void *path)
{
	gchar *text = NULL;
	if (!g_file_get_contents(path, &text, NULL, NULL)) {
		say("no script %s", (char *)path);
		exit(3);
	}
	/* Until main has connected its streams */
	pw_thread_loop_lock(started_loop);
	pw_thread_loop_unlock(started_loop);
	g_auto(GStrv) lines = g_strsplit(text, "\n", -1);
	g_free(text);
	for (int i = 0; lines[i]; i++)
		if (*lines[i] && *lines[i] != '#')
			exec_cmd(lines[i]);
	return NULL;
}

/* ---- Commands ---- */

static struct bridge ub = {
	.route_lock = PTHREAD_MUTEX_INITIALIZER,
	.ref_lock = PTHREAD_MUTEX_INITIALIZER,
	.routed_out = AUDIO_DEVICE_OUT_SPEAKER,
	.speaker_routed = true,
	.out_lock = PTHREAD_MUTEX_INITIALIZER,
	.out_cond = PTHREAD_COND_INITIALIZER,
	.in_lock = PTHREAD_MUTEX_INITIALIZER,
	.in_cond = PTHREAD_COND_INITIALIZER,
	.hal_out_lock = PTHREAD_MUTEX_INITIALIZER,
	.hal_in_lock = PTHREAD_MUTEX_INITIALIZER,
	.fm_lock = PTHREAD_MUTEX_INITIALIZER,
	.fm_cond = PTHREAD_COND_INITIALIZER,
	.sco_restore = -1,
	.role_phone_gain = -1.0f,
};

static struct port *port_by_name(const char *name)
{
	struct port *ports[] = {&B->speaker, &B->earpiece, &B->phones, &B->microphone, &B->bt_mic, &B->fm};
	for (size_t i = 0; i < G_N_ELEMENTS(ports); i++)
		if (!strcmp(ports[i]->name, name))
			return ports[i];
	say("no port %s", name);
	exit(2);
}

static enum pw_stream_state state_by_name(const char *name)
{
	return !strcmp(name, "streaming") ? PW_STREAM_STATE_STREAMING : !strcmp(name, "paused") ? PW_STREAM_STATE_PAUSED
	       : !strcmp(name, "connecting") ? PW_STREAM_STATE_CONNECTING : !strcmp(name, "error") ? PW_STREAM_STATE_ERROR
	       : PW_STREAM_STATE_UNCONNECTED;
}

static void stream_state(const char *node, enum pw_stream_state state, const char *error)
{
	struct pw_stream *s = stream_by_name(node);
	if (s)
		s->events->state_changed(s->data, PW_STREAM_STATE_PAUSED, state, error);
}

static void set_quit(bool quit)
{
	pthread_mutex_lock(&B->out_lock);
	pthread_mutex_lock(&B->in_lock);
	pthread_mutex_lock(&B->fm_lock);
	B->quit = quit;
	pthread_cond_signal(&B->out_cond);
	pthread_cond_signal(&B->in_cond);
	pthread_cond_signal(&B->fm_cond);
	pthread_mutex_unlock(&B->fm_lock);
	pthread_mutex_unlock(&B->in_lock);
	pthread_mutex_unlock(&B->out_lock);
}

/* k=v;k=v as a dictionary (the strings stay with the caller's array) */
static struct spa_dict *parse_dict(const char *text, GStrv *keep)
{
	static __thread struct spa_dict_item items[PROPS_MAX];
	static __thread struct spa_dict dict;
	*keep = g_strsplit(text ? text : "", ";", -1);
	uint32_t n = 0;
	for (int i = 0; (*keep)[i] && n < PROPS_MAX; i++) {
		char *eq = strchr((*keep)[i], '=');
		if (!eq)
			continue;
		*eq = 0;
		items[n].key = (*keep)[i];
		items[n++].value = eq + 1;
	}
	dict = SPA_DICT_INIT(items, n);
	return &dict;
}

static void emit_global(uint32_t id, const char *type, const char *props, bool no_props)
{
	char full[96];
	snprintf(full, sizeof(full), "PipeWire:Interface:%s", type);
	GStrv keep = NULL;
	struct spa_dict *dict = no_props ? NULL : parse_dict(props, &keep);
	spa_hook_list_call(&the_registry->hooks, struct pw_registry_events, global, 0, id, PW_PERM_RWX, full, 3, dict);
	g_strfreev(keep);
}

static uint32_t node_id_of(const char *text)
{
	if (g_ascii_isdigit(*text))
		return atoi(text);
	struct pw_stream *s = stream_by_name(text);
	return s ? s->node_id : SPA_ID_INVALID;
}

static void emit_link(uint32_t id, const char *out, const char *in)
{
	char props[128];
	snprintf(props, sizeof(props), "link.output.node=%u;link.input.node=%u", node_id_of(out), node_id_of(in));
	emit_global(id, "Link", props, false);
}

/* Commands that touch only the bridge's state or PipeWire's events run with the loop locked,
 * as in PipeWire's thread; the others take what they need themselves */
static bool unlocked_cmd(const char *cmd)
{
	static const char *const names[] = {"async", "quit", "unquit", "idle", "fire", "run-playback", "run-capture", "run-fm",
					    "delay", "barrier", "dbus", "dbus-get", "dbus-signals", "dbus-wait", "sigterm",
					    "jack", "hal-sco", "reopen-input", "cycle", "amp", "route", "call", "deadline",
					    "role-volume", "step-volume", "load-volume", "save-volume", "record", "play",
					    "ring", "ref-reset", "active", "open-hal", "report-wired", "phones-update",
					    "process", "trigger-done", "fill"};
	for (size_t i = 0; i < G_N_ELEMENTS(names); i++)
		if (!strcmp(cmd, names[i]))
			return true;
	return false;
}

static void exec_locked(char **a, const char *line);

static void exec_cmd(const char *line)
{
	g_auto(GStrv) head = g_strsplit(line, ":", 2);
	const char *cmd = head[0] ? head[0] : "";
	/* Commands whose last field may hold colons */
	static const struct {
		const char *name;
		int fields;
	} split[] = {{"at", 5}, {"async", 2}, {"delay", 3}, {"fail", 2}, {"meta", 5}, {"global", 4}, {"node-info", 3}, {"dbus", 3},
		     {"state", 4}, {"globals", 5}};
	int fields = -1;
	for (size_t i = 0; i < G_N_ELEMENTS(split); i++)
		if (!strcmp(cmd, split[i].name))
			fields = split[i].fields;
	g_auto(GStrv) a = g_strsplit(line, ":", fields);
	if (unlocked_cmd(cmd) || !B || !B->loop) {
		exec_locked(a, line);
	} else {
		pw_thread_loop_lock(B->loop);
		exec_locked(a, line);
		pw_thread_loop_unlock(B->loop);
	}
}

static void *thread_of(void *(*fn)(void *))
{
	pthread_t t;
	pthread_create(&t, NULL, fn, B);
	pthread_join(t, NULL);
	return NULL;
}

static void print_bt(void)
{
	say("bt sco_state=%d sco_device=%u restore=%d blocked=%d codec=%s sco=%d headset=%s/%u source=%d links=%zu "
	    "default=%s hold=%d",
	    B->sco_state, B->sco_device, B->sco_restore, B->sco_blocked, B->sco_codec, atomic_load(&B->sco), B->headset_name,
	    B->headset_device, B->bt_mic.stream != NULL, B->n_headset_links, B->default_sink, B->sco_hold != NULL);
}

static void exec_locked(char **a, const char *line)
{
	const char *cmd = a[0];
#define ARG(i) (a[i] ? a[i] : "")
	if (!strcmp(cmd, "route")) {
		/* route:sco:headphones:headset_mic:call:call_speaker:playing (SINK_*):aec_on */
		atomic_store(&B->sco, atoi(a[1]));
		B->headphones = atoi(a[2]);
		B->headset_mic = atoi(a[3]);
		B->call = atoi(a[4]);
		B->call_speaker = atoi(a[5]);
		B->playing = atoi(a[6]);
		atomic_store(&B->aec_on, atoi(a[7]));
		route(B);
		say("routed=%#x speaker=%d speakerphone=%d", B->routed_out, atomic_load(&B->speaker_routed),
		    atomic_load(&B->speakerphone));
	} else if (!strcmp(cmd, "ring")) {
		/* ring:<write bytes>,<read bytes>:...: writes, then reads back */
		static struct port p;
		uint8_t *buf = malloc(RING_SIZE * 2), *back = malloc(RING_SIZE * 2);
		uint8_t next = 0, expect = 0;
		spa_ringbuffer_init(&p.ring);
		for (int k = 1; a[k]; k++) {
			g_auto(GStrv) ops = g_strsplit(a[k], ",", -1);
			uint32_t w = atoi(ops[0]), r = ops[1] ? atoi(ops[1]) : 0;
			uint32_t before = ring_fill(&p);
			for (uint32_t j = 0; j < w; j++)
				buf[j] = next + j;
			ring_write(&p, buf, w);
			if (ring_fill(&p) != before)
				next += w;
			uint32_t got = ring_read(&p, back, r);
			int ok = 1;
			for (uint32_t j = 0; j < got; j++)
				ok &= back[j] == (uint8_t)(expect + j);
			expect += got;
			say("ring %s=fill %u read %u in-order %d", a[k], ring_fill(&p), got, ok);
		}
		free(buf);
		free(back);
	} else if (!strcmp(cmd, "play")) {
		/* play:<frames>:<value>: stereo frames to the HAL, as the echo reference */
		size_t n = atoi(a[1]);
		int16_t *s = g_new(int16_t, n * 2);
		for (size_t k = 0; k < n * 2; k++)
			s[k] = atoi(a[2]);
		echo_reference(B, s, n);
		g_free(s);
		say("queued=%zu fill=%zu", B->ref_count, B->render_fill);
	} else if (!strcmp(cmd, "record")) {
		/* record:<frames>: mono microphone frames through the canceller */
		size_t n = atoi(a[1]);
		int16_t *s = g_new0(int16_t, n), *o = g_new0(int16_t, n + AEC_BLOCK);
		size_t bytes = echo_cancel(B, s, n, o);
		GString *refs = g_string_new(NULL);
		for (size_t k = 0; k < bytes / 2; k += AEC_BLOCK)
			g_string_append_printf(refs, "%d,", o[k]);
		say("cancelled bytes=%zu queued=%zu pending=%zu refs=%s", bytes, B->ref_count, B->capture_fill, refs->str);
		g_string_free(refs, TRUE);
		g_free(s);
		g_free(o);
	} else if (!strcmp(cmd, "ref-reset")) {
		echo_reference_reset(B);
	} else if (!strcmp(cmd, "role")) {
		say("role %s=%d", a[1], voice_role(*a[1] ? a[1] : NULL));
	} else if (!strcmp(cmd, "hfp")) {
		say("hfp %s=%d", a[1], hfp_profile(a[1]));
	} else if (!strcmp(cmd, "load-volume")) {
		load_call_volume(B);
		say("call-step=%d", B->call_step);
	} else if (!strcmp(cmd, "save-volume")) {
		B->call_step = atoi(a[1]);
		save_call_volume(B);
	} else if (!strcmp(cmd, "step-volume")) {
		B->call_step = atoi(a[1]);
		apply_call_volume(B, "test");
	} else if (!strcmp(cmd, "role-volume")) {
		/* role-volume:<role gain>:<keys' sink gain, - for a headset>:<start> */
		static struct port sink = {.name = "speaker"};
		if (strcmp(a[2], "-")) {
			atomic_store(&sink.gain, g_ascii_strtod(a[2], NULL));
			atomic_store(&B->volume_port, &sink);
		} else {
			atomic_store(&B->volume_port, NULL);
		}
		apply_role_call_volume(B, g_ascii_strtod(a[1], NULL), atoi(a[3]));
	} else if (!strcmp(cmd, "set")) {
		/* set:<name>:<value>: sizes and samples of the HAL stand-in */
		long v = atol(ARG(2));
		if (!strcmp(a[1], "out_buffer"))
			out_buffer = v;
		else if (!strcmp(a[1], "in_buffer"))
			in_buffer = v;
		else if (!strcmp(a[1], "fm_buffer"))
			fm_buffer = v;
		else if (!strcmp(a[1], "out_latency"))
			out_latency = v;
		else if (!strcmp(a[1], "in_low"))
			in_low = v;
		else if (!strcmp(a[1], "in_high"))
			in_high = v;
		else if (!strcmp(a[1], "fm_value"))
			fm_value = v;
		else if (!strcmp(a[1], "call_step"))
			B->call_step = v;
		else if (!strcmp(a[1], "aec_delay"))
			B->aec_delay = v;
	} else if (!strcmp(cmd, "fail")) {
		add_fails(ARG(1));
	} else if (!strcmp(cmd, "compact")) {
		compact = true;
	} else if (!strcmp(cmd, "at")) {
		/* at:<event>:<n>:<sync|async>:<command> */
		if (n_actions < (int)G_N_ELEMENTS(actions)) {
			snprintf(actions[n_actions].event, sizeof(actions[0].event), "%s", a[1]);
			actions[n_actions].n = atoi(a[2]);
			actions[n_actions].async = !strcmp(a[3], "async");
			snprintf(actions[n_actions++].cmd, sizeof(actions[0].cmd), "%s", a[4]);
		}
	} else if (!strcmp(cmd, "async")) {
		run_async(a[1]);
	} else if (!strcmp(cmd, "delay")) {
		g_usleep(atoi(a[1]) * 1000);
		exec_cmd(a[2]);
	} else if (!strcmp(cmd, "open-hal")) {
		int r = open_hal(B);
		say("open-hal=%d", r);
		if (!r)
			say("chunks out=%zu mic=%zu headset=%zu fm=%zu in-channels=%u has-fm=%d frame-sizes=%u,%u,%u", B->speaker.chunk,
			    B->microphone.chunk, B->bt_mic.chunk, B->fm.chunk, B->in_channels, B->has_fm, B->speaker.frame_size,
			    B->microphone.frame_size, B->fm.frame_size);
	} else if (!strcmp(cmd, "setup")) {
		/* As main does it: the HAL, the three nodes, the registry */
		B->aec = &fake_aec;
		load_call_volume(B);
		if (open_hal(B)) {
			say("setup failed");
			exit(2);
		}
		int speaker = connect_port(B, &B->speaker), earpiece = connect_port(B, &B->earpiece);
		int microphone = connect_port(B, &B->microphone);
		say("setup connect=%d,%d,%d", speaker, earpiece, microphone);
		watch_voice(B);
	} else if (!strcmp(cmd, "connect-port")) {
		say("connect-port %s=%d", a[1], connect_port(B, port_by_name(a[1])));
	} else if (!strcmp(cmd, "state")) {
		stream_state(a[1], state_by_name(a[2]), a[3]);
	} else if (!strcmp(cmd, "on")) {
		stream_state(a[1], PW_STREAM_STATE_STREAMING, NULL);
	} else if (!strcmp(cmd, "off")) {
		stream_state(a[1], PW_STREAM_STATE_PAUSED, NULL);
	} else if (!strcmp(cmd, "stream")) {
		/* stream:<node>:<field>:<value>: what the stand-in stream does */
		struct pw_stream *s = stream_by_name(a[1]);
		long v = atol(ARG(3));
		if (!s)
			return;
		if (!strcmp(a[2], "value"))
			s->value = v;
		else if (!strcmp(a[2], "quantum"))
			s->quantum = v;
		else if (!strcmp(a[2], "requested"))
			s->requested = v;
		else if (!strcmp(a[2], "maxsize"))
			s->maxsize = v;
		else if (!strcmp(a[2], "offset"))
			s->offset = v;
		else if (!strcmp(a[2], "driving"))
			s->driving = v;
		else if (!strcmp(a[2], "nodone"))
			s->no_done = v;
		else if (!strcmp(a[2], "nobuffer"))
			s->no_buffer = v;
		else if (!strcmp(a[2], "nodata"))
			s->no_data = v;
	} else if (!strcmp(cmd, "control")) {
		/* control:<node>:<volume|channels|mute>:<v,v...> */
		struct pw_stream *s = stream_by_name(a[1]);
		float values[8];
		uint32_t n = 0;
		g_auto(GStrv) v = g_strsplit(ARG(3), ",", -1);
		for (int i = 0; v[i] && n < 8; i++)
			if (*v[i])
				values[n++] = g_ascii_strtod(v[i], NULL);
		struct pw_stream_control c = {.name = a[2], .values = values, .n_values = n, .max_values = 8};
		uint32_t id = !strcmp(a[2], "volume") ? SPA_PROP_volume : !strcmp(a[2], "channels") ? SPA_PROP_channelVolumes
			      : SPA_PROP_mute;
		if (s)
			s->events->control_info(s->data, id, &c);
	} else if (!strcmp(cmd, "process")) {
		struct pw_stream *s = stream_by_name(a[1]);
		if (s)
			s->events->process(s->data);
	} else if (!strcmp(cmd, "trigger-done")) {
		struct pw_stream *s = stream_by_name(a[1]);
		if (s)
			s->events->trigger_done(s->data);
	} else if (!strcmp(cmd, "fill")) {
		/* fill:<port>:<bytes>:<value>: samples into a node's ring */
		struct port *p = port_by_name(a[1]);
		uint32_t n = atoi(a[2]);
		int16_t *v = g_new(int16_t, n / 2 + 1);
		for (uint32_t i = 0; i < n / 2; i++)
			v[i] = atoi(a[3]);
		pthread_mutex_lock(p->lock);
		ring_write(p, v, n);
		pthread_mutex_unlock(p->lock);
		g_free(v);
	} else if (!strcmp(cmd, "port")) {
		struct port *p = port_by_name(a[1]);
		say("port %s active=%d cycles=%lu period=%zu fill=%u unvolume=%.3f gain=%.3f channel=%.3f", p->name, p->active,
		    p->cycles, p->period, ring_fill(p), atomic_load(&p->unvolume), atomic_load(&p->gain),
		    atomic_load(&p->channel));
	} else if (!strcmp(cmd, "active")) {
		struct port *p = port_by_name(a[1]);
		pthread_mutex_lock(p->lock);
		p->active = atoi(a[2]);
		pthread_mutex_unlock(p->lock);
	} else if (!strcmp(cmd, "cycle")) {
		struct port *p = port_by_name(a[1]);
		pthread_mutex_lock(p->lock);
		bool r = run_cycle(p);
		pthread_mutex_unlock(p->lock);
		say("cycle %s=%d cycles=%lu", p->name, r, p->cycles);
	} else if (!strcmp(cmd, "drop-stream")) {
		/* The port loses its stream, as the headset's source when its headset goes */
		port_by_name(a[1])->stream = NULL;
	} else if (!strcmp(cmd, "quit")) {
		set_quit(true);
	} else if (!strcmp(cmd, "unquit")) {
		set_quit(false);
	} else if (!strcmp(cmd, "sco")) {
		atomic_store(&B->sco, atoi(a[1]));
	} else if (!strcmp(cmd, "aec")) {
		if (!strcmp(a[1], "null"))
			B->aec = NULL;
		else
			atomic_store(&B->aec_on, atoi(a[1]));
	} else if (!strcmp(cmd, "speakerphone")) {
		atomic_store(&B->speakerphone, atoi(a[1]));
	} else if (!strcmp(cmd, "speaker-routed")) {
		atomic_store(&B->speaker_routed, atoi(a[1]));
	} else if (!strcmp(cmd, "alsa")) {
		/* alsa:<items, comma separated, or - for no control>:<current index> */
		if (!strcmp(a[1], "-")) {
			n_amp_items = -1;
		} else {
			g_auto(GStrv) items = g_strsplit(a[1], ",", 8);
			for (n_amp_items = 0; items[n_amp_items]; n_amp_items++)
				snprintf(amp_items[n_amp_items], sizeof(amp_items[0]), "%s", items[n_amp_items]);
			amp_current = atoi(ARG(2));
		}
	} else if (!strcmp(cmd, "amp")) {
		say("amp-set %s=%d", a[1], set_earpiece_amp(atoi(a[1])));
	} else if (!strcmp(cmd, "deadline")) {
		/* deadline:<nanoseconds of now>: 50 ms later, carried into the seconds */
		struct timespec now, until;
		clock_nsec = atol(a[1]);
		do {
			fake_clock_gettime(CLOCK_REALTIME, &now);
			deadline(&until);
		} while (until.tv_sec > now.tv_sec + 1); /* the second turned in between */
		clock_nsec = -1;
		say("deadline +%lds %ldns", (long)(until.tv_sec - now.tv_sec), until.tv_nsec);
	} else if (!strcmp(cmd, "add-ms")) {
		struct timespec t = {atol(a[1]), atol(a[2])};
		add_ms(&t, atol(a[3]));
		say("add-ms %ld.%09ld", (long)t.tv_sec, t.tv_nsec);
	} else if (!strcmp(cmd, "run-playback")) {
		thread_of(playback_thread);
		say("playback thread ended");
	} else if (!strcmp(cmd, "run-capture")) {
		thread_of(capture_thread);
		say("capture thread ended");
	} else if (!strcmp(cmd, "run-fm")) {
		thread_of(fm_thread);
		say("fm thread ended");
	} else if (!strcmp(cmd, "ref")) {
		pthread_mutex_lock(&B->ref_lock);
		say("ref count=%zu first=%d fill=%zu", B->ref_count, B->ref_count ? B->ref_queue[B->ref_head][0] : 0,
		    B->render_fill);
		pthread_mutex_unlock(&B->ref_lock);
	} else if (!strcmp(cmd, "idle")) {
		while (g_main_context_iteration(NULL, FALSE))
			;
	} else if (!strcmp(cmd, "fire")) {
		if (main_mode) {
			g_main_context_invoke(NULL, fire_timeout, GUINT_TO_POINTER(atoi(a[1])));
			barrier();
		} else {
			fire_timeout(GUINT_TO_POINTER(atoi(a[1])));
		}
	} else if (!strcmp(cmd, "timeouts")) {
		for (size_t i = 0; i < G_N_ELEMENTS(timeouts); i++)
			if (timeouts[i].pending)
				say("timeout pending %u", timeouts[i].ms);
	} else if (!strcmp(cmd, "call")) {
		/* call:<call>:<on speaker>: the call state, and what the bridge does after a change */
		pthread_mutex_lock(&B->route_lock);
		B->call = atoi(a[1]);
		B->call_speaker = atoi(ARG(2));
		pthread_mutex_unlock(&B->route_lock);
		call_volume_schedule(B);
		bt_schedule(B);
	} else if (!strcmp(cmd, "headphones")) {
		pthread_mutex_lock(&B->route_lock);
		B->headphones = atoi(a[1]);
		B->headset_mic = atoi(ARG(2));
		pthread_mutex_unlock(&B->route_lock);
	} else if (!strcmp(cmd, "phones-update")) {
		atomic_store(&B->phones_scheduled, true);
		headphones_update(B);
	} else if (!strcmp(cmd, "jack")) {
		B->jack_fd = open_jack();
		say("jack %s", B->jack_fd >= 0 ? "found" : "none");
		if (B->jack_fd >= 0) {
			jack_thread(B);
			close(B->jack_fd);
		}
	} else if (!strcmp(cmd, "report-wired")) {
		pthread_mutex_lock(&B->route_lock);
		report_wired(B, &B->wired_out, strtoul(a[1], NULL, 0));
		pthread_mutex_unlock(&B->route_lock);
	} else if (!strcmp(cmd, "volume-port")) {
		struct port *p = atomic_load(&B->volume_port);
		say("volume-port=%s", p ? p->name : "none");
	} else if (!strcmp(cmd, "voice")) {
		say("voice aec_on=%d speakerphone=%d mic_links=%zu", atomic_load(&B->aec_on), atomic_load(&B->speakerphone),
		    B->n_mic_links);
	} else if (!strcmp(cmd, "call-volume")) {
		struct port *p = B->call_port;
		say("call-volume step=%d on=%d port=%s media=%.3f role=%.3f applied=%.3f", B->call_step, B->call_on,
		    p ? p->name : "none", B->media_channel, atomic_load(&B->role_phone_gain), B->voice_applied);
	} else if (!strcmp(cmd, "bt")) {
		print_bt();
	} else if (!strcmp(cmd, "global")) {
		/* global:<id>:<type>:<k=v;k=v> */
		emit_global(atoi(a[1]), a[2], ARG(3), false);
	} else if (!strcmp(cmd, "global-noprops")) {
		emit_global(atoi(a[1]), a[2], NULL, true);
	} else if (!strcmp(cmd, "globals")) {
		/* globals:<first id>:<count>:<type>:<props> */
		for (int i = 0; i < atoi(a[2]); i++)
			emit_global(atoi(a[1]) + i, a[3], ARG(4), false);
	} else if (!strcmp(cmd, "link")) {
		/* link:<id>:<output node, number or node name>:<input node> */
		emit_link(atoi(a[1]), a[2], a[3]);
	} else if (!strcmp(cmd, "links")) {
		/* links:<first id>:<count>:<output>:<first input node> */
		for (int i = 0; i < atoi(a[2]); i++) {
			char in[16];
			snprintf(in, sizeof(in), "%d", atoi(a[4]) + i);
			emit_link(atoi(a[1]) + i, a[3], in);
		}
	} else if (!strcmp(cmd, "remove")) {
		spa_hook_list_call(&the_registry->hooks, struct pw_registry_events, global_remove, 0, atoi(a[1]));
	} else if (!strcmp(cmd, "node-info")) {
		/* node-info:<id>:<k=v;k=v> (no props: only the change mask's other bits) */
		struct pw_proxy *o = object_by_id(atoi(a[1]));
		GStrv keep = NULL;
		struct pw_node_info info = {.id = atoi(a[1])};
		if (a[2]) {
			info.change_mask = PW_NODE_CHANGE_MASK_PROPS;
			info.props = parse_dict(a[2], &keep);
		} else {
			info.change_mask = PW_NODE_CHANGE_MASK_STATE;
		}
		if (o)
			spa_hook_list_call(&o->hooks, struct pw_node_events, info, 0, &info);
		g_strfreev(keep);
	} else if (!strcmp(cmd, "dev-info")) {
		/* dev-info:<id>:<param>=<r|->,...  (no list: a change of the properties only) */
		struct pw_proxy *o = object_by_id(atoi(a[1]));
		struct spa_param_info params[8];
		struct pw_device_info info = {.id = atoi(a[1]), .params = params};
		if (a[2]) {
			info.change_mask = PW_DEVICE_CHANGE_MASK_PARAMS;
			g_auto(GStrv) specs = g_strsplit(a[2], ",", 8);
			for (int i = 0; specs[i]; i++) {
				g_auto(GStrv) kv = g_strsplit(specs[i], "=", 2);
				uint32_t id = !strcmp(kv[0], "EnumProfile") ? SPA_PARAM_EnumProfile
					      : !strcmp(kv[0], "Profile") ? SPA_PARAM_Profile : SPA_PARAM_Props;
				params[info.n_params++] = SPA_PARAM_INFO(id, kv[1] && !strcmp(kv[1], "r") ? SPA_PARAM_INFO_READ : 0);
			}
		} else {
			info.change_mask = PW_DEVICE_CHANGE_MASK_PROPS;
		}
		if (o)
			spa_hook_list_call(&o->hooks, struct pw_device_events, info, 0, &info);
	} else if (!strcmp(cmd, "dev-param")) {
		/* dev-param:<id>:<EnumProfile|Profile>:<index>:<name, missing: none> */
		struct pw_proxy *o = object_by_id(atoi(a[1]));
		uint8_t buffer[256];
		struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
		uint32_t id = !strcmp(a[2], "EnumProfile") ? SPA_PARAM_EnumProfile : SPA_PARAM_Profile;
		const struct spa_pod *param;
		if (a[4])
			param = spa_pod_builder_add_object(&builder, SPA_TYPE_OBJECT_ParamProfile, id, SPA_PARAM_PROFILE_index,
							   SPA_POD_Int(atoi(a[3])), SPA_PARAM_PROFILE_name, SPA_POD_String(a[4]));
		else
			param = spa_pod_builder_add_object(&builder, SPA_TYPE_OBJECT_ParamProfile, id, SPA_PARAM_PROFILE_index,
							   SPA_POD_Int(atoi(a[3])));
		if (o)
			spa_hook_list_call(&o->hooks, struct pw_device_events, param, 0, 0, id, 0, 0, param);
	} else if (!strcmp(cmd, "role-props")) {
		/* role-props:<id>:<kind: Props, Other, null, int, empty>:<volume or ->:<channel volumes or -> */
		struct pw_proxy *o = object_by_id(atoi(a[1]));
		uint8_t buffer[512];
		struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
		const struct spa_pod *param = NULL;
		uint32_t id = SPA_PARAM_Props;
		if (!strcmp(a[2], "Other")) {
			id = SPA_PARAM_Format;
			param = spa_pod_builder_add_object(&builder, SPA_TYPE_OBJECT_Props, SPA_PARAM_Props, SPA_PROP_volume,
							   SPA_POD_Float(0.5f));
		} else if (!strcmp(a[2], "int")) {
			spa_pod_builder_int(&builder, 5);
			param = (const struct spa_pod *)buffer;
		} else if (!strcmp(a[2], "Props")) {
			struct spa_pod_frame f;
			spa_pod_builder_push_object(&builder, &f, SPA_TYPE_OBJECT_Props, SPA_PARAM_Props);
			spa_pod_builder_add(&builder, SPA_PROP_mute, SPA_POD_Bool(false), 0);
			if (a[3] && strcmp(a[3], "-"))
				spa_pod_builder_add(&builder, SPA_PROP_volume, SPA_POD_Float(g_ascii_strtod(a[3], NULL)), 0);
			if (a[4] && strcmp(a[4], "-")) {
				g_auto(GStrv) v = g_strsplit(a[4], ",", 8);
				float values[8];
				uint32_t n = 0;
				for (; v[n]; n++)
					values[n] = g_ascii_strtod(v[n], NULL);
				spa_pod_builder_prop(&builder, SPA_PROP_channelVolumes, 0);
				spa_pod_builder_array(&builder, sizeof(float), SPA_TYPE_Float, n, values);
			}
			param = spa_pod_builder_pop(&builder, &f);
		}
		if (o)
			spa_hook_list_call(&o->hooks, struct pw_node_events, param, 0, 0, id, 0, 0, param);
		say("role-phone-gain=%.3f", atomic_load(&B->role_phone_gain));
	} else if (!strcmp(cmd, "meta")) {
		/* meta:<id>:<subject>:<key or ->:<value or -> */
		struct pw_proxy *o = object_by_id(atoi(a[1]));
		const char *key = strcmp(ARG(3), "-") ? a[3] : NULL;
		const char *value = strcmp(ARG(4), "-") ? a[4] : NULL;
		if (o)
			spa_hook_list_call(&o->hooks, struct pw_metadata_events, property, 0, atoi(a[2]), key, "Spa:String:JSON",
					   value);
	} else if (!strcmp(cmd, "hal-sco")) {
		hal_sco(B, atoi(a[1]), atoi(ARG(2)));
	} else if (!strcmp(cmd, "reopen-input")) {
		pthread_mutex_lock(&B->route_lock);
		reopen_input(B);
		pthread_mutex_unlock(&B->route_lock);
		say("in-channels=%u", B->in_channels);
	} else if (!strcmp(cmd, "property")) {
		/* property:<name>: the property getter itself (GDBus refuses names outside the
		 * introspection before it is called) */
		GError *error = NULL;
		GVariant *v = on_call_audio_get(NULL, NULL, NULL, NULL, a[1], &error, B);
		say_reply(a[1], v ? g_variant_ref_sink(v) : NULL, error);
	} else if (!strcmp(cmd, "barrier")) {
		if (main_mode)
			barrier();
		else
			while (g_main_context_iteration(NULL, FALSE))
				;
	} else if (!strcmp(cmd, "dbus-wait")) {
		client_connect();
	} else if (!strcmp(cmd, "dbus")) {
		/* dbus:<method>:<arguments as GVariant text> */
		GError *error = NULL;
		GVariant *args = g_variant_parse(NULL, a[2], NULL, NULL, &error);
		if (!args) {
			say("bad arguments %s: %s", a[2], error->message);
			exit(2);
		}
		GVariant *r = client_call("org.mobian_project.CallAudio", a[1], args, &error);
		say_reply(a[1], r, error);
	} else if (!strcmp(cmd, "dbus-get")) {
		GError *error = NULL;
		GVariant *r = client_call("org.freedesktop.DBus.Properties", "Get",
					  g_variant_new("(ss)", "org.mobian_project.CallAudio", a[1]), &error);
		say_reply(a[1], r, error);
	} else if (!strcmp(cmd, "dbus-signals")) {
		/* dbus-signals:<n>: waits until n signals came in all */
		for (int i = 0; i < 500 && n_signals < atoi(a[1]); i++) {
			while (g_main_context_iteration(client_context, FALSE))
				;
			if (n_signals < atoi(a[1]))
				g_usleep(10000);
		}
		say("signals=%d", n_signals);
	} else if (!strcmp(cmd, "sigterm")) {
		kill(getpid(), SIGTERM);
	} else {
		say("unknown command %s", line);
		exit(2);
	}
#undef ARG
}

/* abort() ends the bridge on purpose (the service restarts it). It comes from the bridge's own
 * code, not asynchronously: exit() here writes the coverage counters of a gcov build too. */
static void on_abort(int sig)
{
	flush_line();
	printf("aborted\n");
	fflush(stdout);
	exit(134);
}

int main(int argc, char **argv)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	signal(SIGABRT, on_abort);
	int first = 1;
	if (argc > 1 && !strcmp(argv[1], "main")) {
		main_mode = true;
		first = 2;
	} else {
		B = &ub;
		describe_ports(&ub);
		atomic_store(&ub.volume_port, &ub.speaker);
		ub.dev = &fake_dev;
		ub.out = &new_out(AUDIO_DEVICE_OUT_SPEAKER)->s;
		ub.in = &new_in(AUDIO_DEVICE_IN_BUILTIN_MIC, AUDIO_CHANNEL_IN_STEREO)->s;
		ub.in_channels = 2;
		ub.aec = &fake_aec;
		ub.loop = pw_thread_loop_new("test", NULL);
		ub.jack_fd = -1;
	}
	for (int i = first; i < argc; i++)
		exec_cmd(argv[i]);
	int r = 0;
	if (main_mode) {
		char *args[] = {"audio-hal-bridge", NULL};
		r = real_main(1, args);
		say("main=%d", r);
	}
	pthread_mutex_lock(&say_lock);
	flush_line();
	pthread_mutex_unlock(&say_lock);
	return r;
}
