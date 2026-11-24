// SPDX-License-Identifier: MIT
/*
 * plinius-torch: the flash LEDs of the Volla Phone Plinius as LED class device "white:torch"
 *
 * Phosh's torch quick setting switches LED class devices named *:torch (through logind's
 * SetBrightness). The flash driver (ocp81378) only offers V4L2 flash subdevices, so this
 * daemon creates the LED through uleds and turns its brightness into the torch intensity of
 * the subdevices. Phosh looks for the LED only when it starts, so the daemon creates it early
 * in the boot; the flash driver comes later with the Android container, and the daemon looks
 * for its subdevices each time the torch goes on.
 *
 * Protecting the LEDs (other phones burnt theirs in torch mode):
 * - torch mode only, never flash mode (high current pulses);
 * - at most --max-current per LED: default 98.9 mA, what Volla OS's camera HAL gives its torch
 *   (measured: only LED 0, 98.9 mA, at most 198.7 mA at the highest strength level; docs/
 *   HARDWARE.md). Both LEDs at that current: no LED gets more than under Volla OS, about as
 *   bright as its highest level;
 * - off after --timeout (default 15 minutes) and before suspend;
 * - the driver keeps the LEDs on when the subdevices are closed, also after a crash:
 *   the service runs "plinius-torch --off" after every stop (ExecStopPost), and the
 *   daemon switches them off when it starts.
 *
 * Phosh reads the brightness only after switching it itself. Others switch it too (the
 * assistant button through logind, the timeout here), so the daemon sends a "change" uevent
 * for the LED after every change; Phosh (patch 0023) then updates its quick setting.
 *
 * Usage: plinius-torch [--max-current uA] [--timeout s] [--levels n] [--match name]
 *        plinius-torch --off [--match name]   (switch the LEDs off and exit)
 */
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <limits.h>
#include <linux/uleds.h>
#include <linux/videodev2.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <systemd/sd-bus.h>
#include <systemd/sd-daemon.h>
#include <systemd/sd-event.h>
#include <unistd.h>

#define LED_NAME "white:torch"
#define MAX_LEDS 4

static int max_current = 98900; /* uA per LED */
static int timeout_s = 15 * 60;
static int levels = 10;
static const char *match = "ocp81378-led";

/* Device paths of the flash LEDs */
static char subdevs[MAX_LEDS][NAME_MAX + 6];
static int n_subdevs;
static int fds[MAX_LEDS] = {-1, -1, -1, -1};
static bool lit;
static sd_event_source *off_timer;

static void find_subdevs(void);

static int set_ctrl(int fd, unsigned int id, int value)
{
	struct v4l2_control c = {.id = id, .value = value};

	return ioctl(fd, VIDIOC_S_CTRL, &c);
}

/* Torch intensity for a brightness level: linear up to the cap, within the driver's range */
static int intensity(int fd, int level)
{
	struct v4l2_queryctrl q = {.id = V4L2_CID_FLASH_TORCH_INTENSITY};
	int cap = max_current, value;

	if (ioctl(fd, VIDIOC_QUERYCTRL, &q) < 0)
		return -1;
	if (cap > q.maximum)
		cap = q.maximum;
	value = (long long)cap * level / levels;
	return value < q.minimum ? q.minimum : value;
}

static void torch_off(void)
{
	for (int i = 0; i < n_subdevs; i++) {
		if (fds[i] < 0)
			continue;
		set_ctrl(fds[i], V4L2_CID_FLASH_LED_MODE, V4L2_FLASH_LED_MODE_NONE);
		close(fds[i]);
		fds[i] = -1;
	}
	lit = false;
	if (off_timer)
		sd_event_source_set_enabled(off_timer, SD_EVENT_OFF);
}

static void torch_on(int level)
{
	/* The subdevices may have appeared (or gone with the container) since the torch was last on */
	if (!lit)
		find_subdevs();
	if (!n_subdevs)
		fprintf(stderr, "no V4L2 subdevice named %s* (yet)\n", match);
	lit = true;
	for (int i = 0; i < n_subdevs; i++) {
		const char *path = subdevs[i];
		int value;

		if (fds[i] < 0 && (fds[i] = open(path, O_RDWR | O_CLOEXEC)) < 0) {
			fprintf(stderr, "%s: %s\n", path, strerror(errno));
			continue;
		}
		value = intensity(fds[i], level);
		if (value < 0 || set_ctrl(fds[i], V4L2_CID_FLASH_TORCH_INTENSITY, value) < 0 ||
		    set_ctrl(fds[i], V4L2_CID_FLASH_LED_MODE, V4L2_FLASH_LED_MODE_TORCH) < 0)
			fprintf(stderr, "%s: cannot switch on: %s\n", path, strerror(errno));
	}
	sd_event_source_set_time_relative(off_timer, (uint64_t)timeout_s * 1000000);
	sd_event_source_set_enabled(off_timer, SD_EVENT_ONESHOT);
}

/*
 * Switches off through the LED's sysfs file, so the brightness Phosh reads matches; uleds
 * then reports level 0 and torch_off() runs from there.
 */
static void request_off(const char *why)
{
	FILE *f = fopen("/sys/class/leds/" LED_NAME "/brightness", "w");

	fprintf(stderr, "switching off: %s\n", why);
	if (f) {
		fputs("0\n", f);
		fclose(f);
	}
	torch_off();
}

/* Tells udev listeners (Phosh) that the brightness changed; sysfs itself does not */
static void announce_change(void)
{
	FILE *f = fopen("/sys/class/leds/" LED_NAME "/uevent", "w");

	if (!f) {
		fprintf(stderr, "cannot announce the change: %s\n", strerror(errno));
		return;
	}
	fputs("change\n", f);
	fclose(f);
}

static int on_level(sd_event_source *s, int fd, uint32_t revents, void *userdata)
{
	int level;

	if (read(fd, &level, sizeof(level)) != sizeof(level))
		return 0;
	if (level <= 0)
		torch_off();
	else
		torch_on(level > levels ? levels : level);
	announce_change();
	return 0;
}

static int on_timeout(sd_event_source *s, uint64_t usec, void *userdata)
{
	request_off("timeout");
	return 0;
}

static int on_prepare_for_sleep(sd_bus_message *m, void *userdata, sd_bus_error *error)
{
	int sleeping = 0;

	if (sd_bus_message_read(m, "b", &sleeping) >= 0 && sleeping)
		request_off("suspend");
	return 0;
}

/* The flash LEDs: V4L2 subdevices whose name starts with --match */
static void find_subdevs(void)
{
	DIR *d = opendir("/sys/class/video4linux");
	struct dirent *e;

	n_subdevs = 0;
	while (d && (e = readdir(d)) && n_subdevs < MAX_LEDS) {
		char path[300], name[64] = "";
		FILE *f;

		if (strncmp(e->d_name, "v4l-subdev", 10))
			continue;
		snprintf(path, sizeof(path), "/sys/class/video4linux/%s/name", e->d_name);
		if (!(f = fopen(path, "r")))
			continue;
		if (fgets(name, sizeof(name), f) && !strncmp(name, match, strlen(match)))
			snprintf(subdevs[n_subdevs++], sizeof(subdevs[0]), "/dev/%s", e->d_name);
		fclose(f);
	}
	if (d)
		closedir(d);
}

int main(int argc, char **argv)
{
	static const struct option opts[] = {
		{"max-current", required_argument, NULL, 'c'},
		{"timeout", required_argument, NULL, 't'},
		{"levels", required_argument, NULL, 'l'},
		{"match", required_argument, NULL, 'm'},
		{"off", no_argument, NULL, 'o'},
		{},
	};
	struct uleds_user_dev dev = {.name = LED_NAME};
	sd_event *event = NULL;
	sd_bus *bus = NULL;
	bool off_only = false;
	int opt, uleds;

	while ((opt = getopt_long(argc, argv, "c:t:l:m:o", opts, NULL)) != -1) {
		switch (opt) {
		case 'c': max_current = atoi(optarg); break;
		case 't': timeout_s = atoi(optarg); break;
		case 'l': levels = atoi(optarg); break;
		case 'm': match = optarg; break;
		case 'o': off_only = true; break;
		default: return 2;
		}
	}
	if (max_current <= 0 || timeout_s <= 0 || levels <= 0)
		return 2;

	/* Whatever a previous run left behind; without the subdevices there is nothing to switch off */
	find_subdevs();
	for (int i = 0; i < n_subdevs; i++)
		fds[i] = open(subdevs[i], O_RDWR | O_CLOEXEC);
	torch_off();
	if (off_only)
		return 0;

	uleds = open("/dev/uleds", O_RDWR | O_CLOEXEC);
	dev.max_brightness = levels;
	if (uleds < 0 || write(uleds, &dev, sizeof(dev)) != sizeof(dev)) {
		perror("/dev/uleds");
		return 1;
	}

	sd_event_default(&event);
	sd_event_add_io(event, NULL, uleds, EPOLLIN, on_level, NULL);
	/* Armed by torch_on(); disabled before the loop runs, so time 0 never fires */
	if (sd_event_add_time(event, &off_timer, CLOCK_BOOTTIME, 0, 0, on_timeout, NULL) < 0) {
		fprintf(stderr, "cannot create the off timer\n");
		return 1;
	}
	sd_event_source_set_enabled(off_timer, SD_EVENT_OFF);
	if (sd_bus_open_system(&bus) >= 0) {
		sd_bus_match_signal(bus, NULL, "org.freedesktop.login1", "/org/freedesktop/login1",
				    "org.freedesktop.login1.Manager", "PrepareForSleep",
				    on_prepare_for_sleep, NULL);
		sd_bus_attach_event(bus, event, SD_EVENT_PRIORITY_NORMAL);
	}
	/* Off on SIGTERM/SIGINT: sd_event exits the loop, the LEDs are switched off below */
	sd_event_set_signal_exit(event, true);

	fprintf(stderr, "%s ready (%d LED(s) present), at most %d uA each, off after %d s\n", LED_NAME,
		n_subdevs, max_current, timeout_s);
	sd_notify(0, "READY=1");
	sd_event_loop(event);
	torch_off();
	return 0;
}
