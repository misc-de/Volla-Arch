// plinius-backlight-wake [CONNECTOR [LED]]: sets the backlight again whenever the display is
// switched on. On the Plinius the MediaTek panel driver leaves the backlight dark when the display
// comes back from power-off; under Android its lights HAL sets it again.
// No event reports the switch to user space: the compositor's output power control belongs to
// Phosh alone, Phosh does not publish it (PowerSaveMode only follows explicit requests), and the
// kernel sends no uevent. So this reads the DRM connector's dpms attribute four times a second
// (one open file, no processes) and, on the change from Off to On, sets the LED to its value minus
// one and back: the LED core skips writes of an unchanged value.
// A kernel module on MediaTek's display notifier would do this without polling (docs/HARDWARE.md).
// Since it knows the display state anyway, it also runs the programs in HOOK_DIR on each change,
// with the argument "on" or "off" (in name order, one after the other; they must be quick), and
// once at start with the current state. The device package uses this to power NFC with the display.
// While Phosh shows the always-on display ($XDG_RUNTIME_DIR/plinius-aod exists), the display counts
// as off: the panel dozes, and its CRTC comes on for a moment each minute for the clock. The panel
// also needs its backlight set again after that. An always-on display can be over in less than two
// polls (240 ms measured with quick presses of the power key), so inotify reports the flag.
// The display being on, it also keeps the screen colour the light sensor's firmware works with: see
// ALS_COLOR and ALS_FILE.
// SPDX-License-Identifier: MIT
#include <dirent.h>
#include <fcntl.h>
#include <glob.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/inotify.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define HOOK_DIR "/usr/lib/plinius/display.d"
// Created by phosh-plinius while the always-on display is shown
#define USER_DIRS "/run/user/*"
#define AOD_FLAG_NAME "plinius-aod"
#define AOD_FLAG "/run/user/*/" AOD_FLAG_NAME
// Written by phoc-plinius: the colour shown over the light sensor, in the driver's format
#define ALS_FILE_NAME "plinius-als-color"
#define ALS_FILE "/run/user/*/" ALS_FILE_NAME

/*
 * The light sensor sits behind the panel. Its firmware subtracts the display's own light, from the
 * brightness and from the colour of the screen. Under Android system_server tells it the colour;
 * here nobody does, and the firmware then subtracts about as much as for a white screen. Measured
 * with a dark screen: 190 lux in the room read as 0 at 90 % brightness and as 77 at 60 %, so auto
 * brightness went up and down all the time with nothing changing in the room.
 * The right colour depends on what is shown over the sensor: phoc-plinius reads that part of the
 * screen and writes the colour to ALS_FILE in the session's runtime directory; with several
 * sessions the newest file counts. Without one (no session, a client's buffer scanned out
 * directly) a fixed value in between keeps the error small both ways.
 * The driver forgets the colour whenever the display goes off.
 */
#define ALS_COLOR_PATH "/sys/kernel/pix_manager/als_color"
// Exactly this, without a newline (with one the blue value is not taken); it also reads back like this
#define ALS_COLOR "A:255R:128G:128B:128"
#define ALS_COLOR_MAX sizeof("A:255R:255G:255B:255")

static char als_color[ALS_COLOR_MAX] = ALS_COLOR;

#define INTERVAL_MS 250

/*
 * An incoming call ends the always-on display by switching the panel off, and it comes back about
 * 290 ms later (measured): too quick for the poll, and the screen then stays dark with a working
 * touch screen, which answered a call by accident. So the backlight is set once more this long after
 * an always-on display has ended, when the panel has settled either way.
 */
#define AOD_SETTLE_MS 500

static long long now_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000LL + ts.tv_nsec / 1000000;
}

static int read_text(int fd, char *buf, size_t len)
{
	ssize_t n = pread(fd, buf, len - 1, 0);
	if (n < 0)
		return -1;
	buf[n] = 0;
	buf[strcspn(buf, "\n")] = 0;
	return 0;
}

static int write_value(const char *path, int value)
{
	char buf[16];
	int fd = open(path, O_WRONLY | O_CLOEXEC);
	if (fd < 0)
		return -1;
	int n = snprintf(buf, sizeof(buf), "%d\n", value);
	int r = write(fd, buf, n) == n ? 0 : -1;
	close(fd);
	return r;
}

static int aod_shown(void)
{
	glob_t g;
	int shown = glob(AOD_FLAG, GLOB_NOSORT, NULL, &g) == 0 && g.gl_pathc > 0;
	globfree(&g);
	return shown;
}

// Watches the runtime directories for the flag. They come and go with the sessions, and adding a
// watch that exists changes nothing, so this simply runs with each poll.
static void watch_flag_dirs(int inotify)
{
	glob_t g;

	if (inotify >= 0 && glob(USER_DIRS, GLOB_NOSORT, NULL, &g) == 0) {
		for (size_t i = 0; i < g.gl_pathc; i++)
			inotify_add_watch(inotify, g.gl_pathv[i],
					  IN_CREATE | IN_DELETE | IN_CLOSE_WRITE | IN_MOVED_TO | IN_ONLYDIR);
		globfree(&g);
	}
}

// Takes the events since the last call: 1 if an always-on display has ended. *als_changed becomes
// 1 if the colour file was written or removed.
static int read_events(int inotify, int *als_changed)
{
	char buf[4096] __attribute__((aligned(__alignof__(struct inotify_event))));
	int ended = 0;
	ssize_t n;

	while (inotify >= 0 && (n = read(inotify, buf, sizeof(buf))) > 0) {
		for (char *p = buf; p < buf + n;) {
			const struct inotify_event *event = (const struct inotify_event *)p;

			if (event->len && (event->mask & IN_DELETE) && strcmp(event->name, AOD_FLAG_NAME) == 0)
				ended = 1;
			if (event->len && (event->mask & (IN_DELETE | IN_CLOSE_WRITE | IN_MOVED_TO)) &&
			    strcmp(event->name, ALS_FILE_NAME) == 0)
				*als_changed = 1;
			p += sizeof(*event) + event->len;
		}
	}
	return ended;
}

// The colour if text is one in the driver's format, each value 0 to 255: written into out as the
// driver reads it back (no leading zeros, no newline). 0 if so, -1 if not.
static int parse_als_color(const char *text, char *out, size_t len)
{
	unsigned a, r, g, b;
	int end = -1;

	if (sscanf(text, "A:%3uR:%3uG:%3uB:%3u%n", &a, &r, &g, &b, &end) != 4 || text[end] ||
	    a > 255 || r > 255 || g > 255 || b > 255)
		return -1;
	snprintf(out, len, "A:%uR:%uG:%uB:%u", a, r, g, b);
	return 0;
}

// The colour from the newest ALS_FILE, ALS_COLOR without a valid one
static void load_als_color(void)
{
	char best[ALS_COLOR_MAX] = ALS_COLOR;
	struct timespec newest = {0, 0};
	glob_t g;

	if (glob(ALS_FILE, GLOB_NOSORT, NULL, &g) == 0) {
		for (size_t i = 0; i < g.gl_pathc; i++) {
			char text[64], color[ALS_COLOR_MAX];
			struct stat st;
			int fd = open(g.gl_pathv[i], O_RDONLY | O_CLOEXEC | O_NOFOLLOW);

			if (fd < 0)
				continue;
			if (fstat(fd, &st) == 0 && S_ISREG(st.st_mode) &&
			    (st.st_mtim.tv_sec > newest.tv_sec ||
			     (st.st_mtim.tv_sec == newest.tv_sec && st.st_mtim.tv_nsec > newest.tv_nsec)) &&
			    read_text(fd, text, sizeof(text)) == 0 &&
			    parse_als_color(text, color, sizeof(color)) == 0) {
				memcpy(best, color, sizeof(best));
				newest = st.st_mtim;
			}
			close(fd);
		}
	}
	globfree(&g);
	memcpy(als_color, best, sizeof(als_color));
}

// Sets the colour if it isn't there; a device without the driver has nothing to do here
static void keep_als_color(void)
{
	static int fd = -1;
	static int warned;
	char color[64];

	if (fd < 0)
		fd = open(ALS_COLOR_PATH, O_RDWR | O_CLOEXEC);
	if (fd < 0 || read_text(fd, color, sizeof(color)) < 0 || strcmp(color, als_color) == 0)
		return;
	if (pwrite(fd, als_color, strlen(als_color), 0) < 0 && !warned) {
		perror("plinius-backlight-wake: " ALS_COLOR_PATH);
		warned = 1;
	}
}

static void run_hooks(int on)
{
	struct dirent **names;
	int n = scandir(HOOK_DIR, &names, NULL, alphasort);
	for (int i = 0; i < n; i++) {
		char path[512];
		struct stat st;
		snprintf(path, sizeof(path), "%s/%s", HOOK_DIR, names[i]->d_name);
		if (stat(path, &st) == 0 && S_ISREG(st.st_mode) && access(path, X_OK) == 0) {
			pid_t pid = fork();
			if (pid == 0) {
				execl(path, path, on ? "on" : "off", (char *)NULL);
				_exit(127);
			}
			if (pid > 0)
				waitpid(pid, NULL, 0);
		}
		free(names[i]);
	}
	if (n >= 0)
		free(names);
}

int main(int argc, char **argv)
{
	const char *connector = argc > 1 ? argv[1] : "card0-DSI-1";
	const char *led = argc > 2 ? argv[2] : "lcd-backlight";
	char dpms_path[256], brightness_path[256], state[32], value[32];
	snprintf(dpms_path, sizeof(dpms_path), "/sys/class/drm/%s/dpms", connector);
	snprintf(brightness_path, sizeof(brightness_path), "/sys/class/leds/%s/brightness", led);

	int dpms = open(dpms_path, O_RDONLY | O_CLOEXEC);
	int brightness = open(brightness_path, O_RDONLY | O_CLOEXEC);
	if (dpms < 0 || brightness < 0) {
		fprintf(stderr, "plinius-backlight-wake: %s or %s missing\n", dpms_path, brightness_path);
		return 1;
	}
	int was_on = read_text(dpms, state, sizeof(state)) == 0 && strcmp(state, "On") == 0 && !aod_shown();
	run_hooks(was_on);
	load_als_color();
	// Without inotify this still polls: poll() skips a negative fd
	int inotify = inotify_init1(IN_NONBLOCK | IN_CLOEXEC);
	struct pollfd pfd = {.fd = inotify, .events = POLLIN};
	int ended = 0;
	long long settle_at = 0;
	for (;;) {
		int als_changed = 0;

		watch_flag_dirs(inotify);
		poll(&pfd, 1, INTERVAL_MS);
		if (read_events(inotify, &als_changed)) {
			ended = 1;
			settle_at = now_ms() + AOD_SETTLE_MS;
		}
		if (als_changed)
			load_als_color();
		if (read_text(dpms, state, sizeof(state)) < 0)
			continue;
		int on = strcmp(state, "On") == 0 && !aod_shown();
		int settled = settle_at && now_ms() >= settle_at;
		if (on && (!was_on || ended || settled) && read_text(brightness, value, sizeof(value)) == 0) {
			int v = 0;
			sscanf(value, "%d", &v);
			// 0 means dark on purpose
			if (v > 0 && (write_value(brightness_path, v - 1) < 0 || write_value(brightness_path, v) < 0))
				perror("plinius-backlight-wake: brightness");
		}
		if (on) {
			ended = 0;
			if (settled)
				settle_at = 0;
			keep_als_color();
		}
		if (on != was_on)
			run_hooks(on);
		was_on = on;
	}
}
