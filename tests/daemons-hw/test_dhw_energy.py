# plinius-energy: energy model and its calibration, per-thread energy from time_in_state, the
# names and keys of cgroups (which app a process counts for), battery drain, the usage file, and
# whole samples over a fake cgroup tree, and the daemon with MCE's display state from a fake on a
# private bus. /sys, /proc and the output files are directories in tmp.

import functools
import os

import pytest

from dhw_daemon_bus import Daemon, FakeService, PrivateBus, poll, sanitized_env
from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "plinius-energy" / "plinius-energy.c"

HARNESS = r"""
#include <glib.h>
#include <gio/gio.h>
#include <stdio.h>
#include <stdlib.h>
#include <errno.h>
#include <sys/stat.h>
/* FAKE_CHMOD_FAILS: the file system refuses the mode change */
static int test_chmod(const char *path, mode_t mode)
{
	if (getenv("FAKE_CHMOD_FAILS")) {
		errno = EPERM;
		return -1;
	}
	return chmod(path, mode);
}
#define chmod test_chmod
#define main real_main
#include "plinius-energy.c"
#undef main
#undef chmod

static void put(const char *rel, const char *text)
{
	g_autofree char *path = g_build_filename(BASE, rel, NULL);
	g_autofree char *dir = g_path_get_dirname(path);
	g_mkdir_with_parents(dir, 0755);
	g_file_set_contents(path, text, -1, NULL);
}

static void print_file(const char *path)
{
	g_autofree char *text = NULL;
	if (g_file_get_contents(path, &text, NULL, NULL))
		fputs(text, stdout);
}

int main(int argc, char **argv)
{
	/* main: the daemon itself, on the bus DBUS_SYSTEM_BUS_ADDRESS names */
	if (argc > 1 && !strcmp(argv[1], "main"))
		return real_main(argc - 1, argv + 1);
	ms_per_tick = 10;
	threads = g_hash_table_new_full(g_direct_hash, g_direct_equal, NULL, g_free);
	cgroups = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, (GDestroyNotify)cgroup_state_free);
	hours = g_hash_table_new_full(g_int64_hash, g_int64_equal, g_free, (GDestroyNotify)g_hash_table_unref);
	g_autoptr(GKeyFile) conf = g_key_file_new();
	if (!g_key_file_load_from_file(conf, DEVICE_CONF, G_KEY_FILE_NONE, NULL))
		g_clear_pointer(&conf, g_key_file_unref);
	gboolean have_model = FALSE;

	for (int i = 1; i < argc; i++) {
		const char *a = argv[i];
		g_auto(GStrv) p = g_strsplit(a, "|", -1);
		if (!strcmp(p[0], "calib")) {
			printf("calib %s %s=%.4f\n", p[1], p[2], calibration(conf, atoi(p[1]), atoi(p[2])));
		} else if (!strcmp(p[0], "em")) {
			have_model = load_energy_model();
			printf("model=%d\n", have_model);
			for (guint c = 0; c < clusters->len; c++) {
				Cluster *cl = g_ptr_array_index(clusters, c);
				printf("cluster %u=", cl->first_cpu);
				for (guint s = 0; s < cl->states->len; s++) {
					PowerState *ps = &g_array_index(cl->states, PowerState, s);
					printf("%u:%.2f ", ps->khz, ps->mw);
				}
				printf("\n");
			}
		} else if (!strcmp(p[0], "power")) {
			printf("power %s %s=%.3f\n", p[1], p[2], cluster_power(find_cluster(atoi(p[1])), atoi(p[2])));
		} else if (!strcmp(p[0], "thread")) {
			double uj = -1, ms = -1;
			gboolean ok = read_thread(p[1], &uj, &ms);
			printf("thread %s=%d %.1f %.1f\n", p[1], ok, uj, ms);
		} else if (!strcmp(p[0], "name")) {
			g_autofree char *n = process_name(p[1]);
			printf("name %s=%s\n", p[1], n ? n : "(null)");
		} else if (!strcmp(p[0], "key")) {
			g_autofree char *k = cgroup_key(p[1], p[2] && *p[2] ? p[2] : NULL);
			printf("key %s=%s\n", p[1], k);
		} else if (!strcmp(p[0], "battery")) {
			find_battery();
			printf("battery=%s\n", self.battery_dir ? self.battery_dir + strlen(BASE) : "(none)");
		} else if (!strcmp(p[0], "drain")) {
			/* drain|status|charge µAh|voltage µV|elapsed ms */
			g_autofree char *s = g_strdup_printf("%s\n", p[1]);
			g_autofree char *c = g_strdup_printf("%s\n", p[2]);
			g_autofree char *v = g_strdup_printf("%s\n", p[3]);
			put("power_supply/battery/status", s);
			put("power_supply/battery/charge_counter", c);
			put("power_supply/battery/voltage_now", v);
			double dms = -1;
			double mj = battery_drain_mj(g_ascii_strtod(p[4], NULL), &dms);
			printf("drain=%.3f %.0f\n", mj, dms);
		} else if (!strcmp(p[0], "drain-again")) {
			/* drain-again|elapsed ms: with the battery's files as they are */
			double dms = -1;
			double mj = battery_drain_mj(g_ascii_strtod(p[1], NULL), &dms);
			printf("drain=%.3f %.0f\n", mj, dms);
		} else if (!strcmp(p[0], "since-boot")) {
			count_since_boot = atoi(p[1]);
		} else if (!strcmp(p[0], "put")) {
			put(p[1], p[2]);
		} else if (!strcmp(p[0], "rmdir")) {
			g_autofree char *path = g_build_filename(BASE, p[1], NULL);
			g_autofree char *f1 = g_build_filename(path, "cpu.stat", NULL);
			g_autofree char *f2 = g_build_filename(path, "cgroup.threads", NULL);
			remove(f1);
			remove(f2);
			if (rmdir(path) < 0)
				printf("rmdir failed\n");
		} else if (!strcmp(p[0], "rm")) {
			g_autofree char *path = g_build_filename(BASE, p[1], NULL);
			remove(path);
		} else if (!strcmp(p[0], "saved-hour")) {
			self.last_saved_hour = g_ascii_strtoll(p[1], NULL, 10);
		} else if (!strcmp(p[0], "screen")) {
			self.screen_on = atoi(p[1]);
		} else if (!strcmp(p[0], "sample")) {
			if (!have_model)
				have_model = load_energy_model();
			sample();
		} else if (!strcmp(p[0], "run-file")) {
			print_file(RUN_FILE);
		} else if (!strcmp(p[0], "add")) {
			/* add|hour|key|on|mj|ms|name */
			usage_add(g_ascii_strtoll(p[1], NULL, 10), p[2], atoi(p[3]), g_ascii_strtod(p[4], NULL),
				  g_ascii_strtod(p[5], NULL), p[6] && *p[6] ? p[6] : NULL);
		} else if (!strcmp(p[0], "prune")) {
			prune_hours(g_ascii_strtoll(p[1], NULL, 10));
		} else if (!strcmp(p[0], "write")) {
			GError *error = NULL;
			g_autofree char *path = g_build_filename(BASE, p[1], NULL);
			printf("write=%d\n", write_usage(path, &error));
			print_file(path);
		} else if (!strcmp(p[0], "load")) {
			g_autofree char *path = g_build_filename(BASE, p[1], NULL);
			load_usage(path);
		}
	}
	return 0;
}
"""


def build(d, group):
    sandboxed_source(SRC, d, [
        ("/sys/kernel/debug/energy_model", d / "em"),
        ("/usr/share/plinius/device.conf", d / "device.conf"),
        ("/sys/fs/cgroup", d / "cgroup"),
        ("/sys/class/power_supply", d / "power_supply"),
        ("/run/plinius-energy/usage", d / "run-usage"),
        ("/var/lib/plinius-energy/usage", d / "state-usage"),
        ("/proc/%s/", f"{d}/proc/%s/"),
        # The test path is longer than the 64 bytes the daemon reserves for /proc/<tid>/...
        ("char path[64], buf[2048];", "char path[4096], buf[2048];"),
        # The group Settings reads through: by default one that does not exist, so nothing is
        # ever chowned
        ('#define READER_GROUP "wheel"', f'#define READER_GROUP "{group}"'),
        # The daemon tests see the screen-off timer fire
        ("#define INTERVAL_OFF_S 900", "#define INTERVAL_OFF_S 1"),
    ])
    exe = build_harness(d, "energy", HARNESS, flags=[f'-DBASE="{d}/"'], pkgs=("gio-unix-2.0",))
    return d, functools.partial(run, exe)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return build(tmp_path_factory.mktemp("energy"), "plinius-test-no-such-group")


@pytest.fixture(scope="module")
def built_group(tmp_path_factory):
    """Built with the test user's own group as Settings' readers"""
    import grp
    return build(tmp_path_factory.mktemp("energy-group"), grp.getgrgid(os.getgid()).gr_name)


def clean(built):
    d, binary = built
    import shutil
    for sub in ("em", "cgroup", "power_supply", "proc"):
        shutil.rmtree(d / sub, ignore_errors=True)
    for f in ("device.conf", "run-usage", "state-usage", "usage-out"):
        p = d / f
        if p.is_dir():
            p.rmdir()
        p.unlink(missing_ok=True)
    return d, binary


@pytest.fixture
def energy(built):
    return clean(built)


def model(d, cpus):
    """Energy model directory: {first_cpu: {khz: mw}}"""
    for cpu, states in cpus.items():
        for i, (khz, mw) in enumerate(states.items()):
            ps = d / "em" / f"cpu{cpu}" / f"ps:{khz}"
            ps.mkdir(parents=True)
            (ps / "frequency").write_text(f"{khz}\n")
            (ps / "power").write_text(f"{mw}\n")


def test_calibration_interpolates(energy):
    d, binary = energy
    (d / "device.conf").write_text("[Energy]\ncpu0=1000000:1.7,2500000:2.4\ncpu4=1.5\n"
                                   "cpu6=1000000:0,2000000:2\n")
    out = kv(binary("calib|0|500000", "calib|0|1000000", "calib|0|1750000", "calib|0|2500000",
                    "calib|0|3000000", "calib|4|123", "calib|4|3000000", "calib|5|1000000",
                    "calib|6|500000").stdout)
    assert float(out["calib 0 500000"]) == pytest.approx(1.7)      # below the first point
    assert float(out["calib 0 1000000"]) == pytest.approx(1.7)
    assert float(out["calib 0 1750000"]) == pytest.approx(2.05)    # halfway
    assert float(out["calib 0 2500000"]) == pytest.approx(2.4)
    assert float(out["calib 0 3000000"]) == pytest.approx(2.4)     # above the last
    assert float(out["calib 4 123"]) == pytest.approx(1.5)         # one factor for all speeds
    assert float(out["calib 4 3000000"]) == pytest.approx(1.5)
    assert float(out["calib 5 1000000"]) == pytest.approx(1.0)     # no entry
    assert float(out["calib 6 500000"]) == pytest.approx(2.0)      # factor 0 is skipped


def test_calibration_without_conf(energy):
    d, binary = energy
    assert float(kv(binary("calib|0|1000000").stdout)["calib 0 1000000"]) == 1.0


def test_energy_model_load_sort_calibrate(energy):
    d, binary = energy
    model(d, {0: {2000000: 300, 500000: 50, 1000000: 100}, 4: {1500000: 400}})
    (d / "em" / "cpufreq").mkdir()               # not a performance domain
    (d / "em" / "cpu0" / "ps:broken").mkdir()     # no frequency/power: skipped
    (d / "device.conf").write_text("[Energy]\ncpu4=2\n")
    out = kv(binary("em", "power|0|400000", "power|0|750000", "power|0|1500000", "power|0|2500000",
                    "power|4|1").stdout)
    assert out["model"] == "1"
    assert out["cluster 0"] == "500000:50.00 1000000:100.00 2000000:300.00"
    assert out["cluster 4"] == "1500000:800.00"
    assert float(out["power 0 400000"]) == 50
    assert float(out["power 0 750000"]) == 75
    assert float(out["power 0 1500000"]) == 200
    assert float(out["power 0 2500000"]) == 300
    assert float(out["power 4 1"]) == 800


def test_no_energy_model(energy):
    d, binary = energy
    assert kv(binary("em").stdout)["model"] == "0"


def test_read_thread(energy):
    d, binary = energy
    model(d, {0: {500000: 50, 1000000: 100}, 4: {2000000: 400}})
    (d / "proc/42").mkdir(parents=True)
    # 10 ms per tick: 100 ms at 50 mW, 200 ms at 100 mW, 50 ms at 400 mW; cpu9 is unknown
    (d / "proc/42/time_in_state").write_text("cpu0\n500000 10\n1000000 20\ncpu4\n2000000 5\n"
                                            "cpu9\n3000000 1000\n")
    out = kv(binary("em", "thread|42", "thread|43").stdout)
    assert out["thread 42"] == f"1 {100 * 50 + 200 * 100 + 50 * 400:.1f} 350.0"
    assert out["thread 43"].startswith("0 ")


@pytest.mark.parametrize("cmdline,expected", [
    (b"/usr/bin/gnome-maps\0--gapplication-service\0", "gnome-maps"),
    (b"/usr/bin/python3\0/usr/bin/plinius-software\0", "plinius-software"),
    (b"python3.12\0-u\0/opt/x/tool.py\0", "tool.py"),
    (b"/usr/bin/python3\0-c\0import secret; key='hunter2'\0", "python3"),   # inline code: no args
    (b"perl\0-e\0print 1\0", "perl"),
    (b"node\0--eval\0x\0", "node"),
    (b"/bin/sh\0/usr/lib/plinius/display.d/nfc\0on\0", "nfc"),
    (b"/usr/bin/shotwell\0", "shotwell"),        # starts with "sh" but is no shell
    (b"/usr/bin/bash\0", "bash"),                # interpreter without a script
    (b"/usr/bin/gjs-console\0", "gjs-console"),  # not gjs followed by digits
    (b"/usr/bin/lua5.4\0/usr/share/x/main.lua\0", "main.lua"),
    (b"evil\tname\nhere\0", "evil name here"),
    (("/usr/bin/" + "a" * 80).encode() + b"\0", "a" * 64),
    (("/usr/bin/" + "a" * 63 + "ää").encode() + b"\0", "a" * 63),  # never half a character
])
def test_process_name(energy, cmdline, expected):
    d, binary = energy
    (d / "proc/7").mkdir(parents=True)
    (d / "proc/7/cmdline").write_bytes(cmdline)
    assert kv(binary("name|7").stdout)["name 7"] == expected


def test_process_name_kernel_thread(energy):
    d, binary = energy
    (d / "proc/8").mkdir(parents=True)
    (d / "proc/8/cmdline").write_bytes(b"")
    (d / "proc/8/comm").write_text("kworker/u16:3\n")
    out = kv(binary("name|8", "name|9").stdout)
    assert out["name 8"] == "kworker/u16:3"
    assert out["name 9"] == "(null)"


@pytest.mark.parametrize("rel,name,expected", [
    ("", "", "kernel"),
    ("user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-org.gnome.Maps-1234.scope", "",
     "app/org.gnome.Maps"),
    ("user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-firefox\\x2dnightly-77.scope", "",
     "app/firefox-nightly"),
    ("user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-de.cais.Emilia-99.scope", "",
     "app/de.cais.Emilia"),
    ("user.slice/user-1000.slice/user@1000.service/app.slice/"
     "app-dbus-:1.2-org.gnome.Settings.slice/dbus-:1.2-org.gnome.Settings@3.service", "",
     "app/org.gnome.Settings"),
    ("user.slice/user-1000.slice/user@1000.service/app.slice/app-dbus-:1.2-org.gnome.Settings.slice", "",
     "app/org.gnome.Settings"),
    ("user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-gnome\\x2dkeyring\\x2dssh-1100.scope",
     "", "app/gnome-keyring-ssh"),
    ("user.slice/user-1000.slice/user@1000.service/app.slice/app-org.gnome.Evolution\\x2dalarm\\x2dnotify"
     "@autostart.service", "", "app/org.gnome.Evolution-alarm-notify"),
    ("user.slice/user-1000.slice/user@1000.service/session.slice/phosh.service", "", "user/phosh.service"),
    ("user.slice/user-1000.slice/session-2.scope", "", "user/session.scope"),
    ("user.slice/user-1000.slice/user@1000.service/init.scope", "", "user/systemd"),
    ("system.slice/systemd-udevd.service/udev", "", "system/systemd-udevd.service"),
    ("system.slice/plinius-energy.service", "", "system/plinius-energy.service"),
    ("init.scope", "", "system/init.scope"),
    ("lxc.payload.android/uid_1000/pid_123", "surfaceflinger", "android/surfaceflinger"),
    ("lxc.payload.android/uid_1000/pid_123", "", "android/pid_123"),
    # A scope without the -<pid> before the suffix is no app scope of the shell
    ("user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-foo.scope", "",
     "user/app-gnome-foo.scope"),
])
def test_cgroup_key(energy, rel, name, expected):
    d, binary = energy
    assert kv(binary(f"key|{rel}|{name}").stdout)[f"key {rel}".strip()] == expected


def test_battery_drain(energy):
    d, binary = energy
    (d / "power_supply/ac").mkdir(parents=True)
    (d / "power_supply/ac/type").write_text("Mains\n")
    (d / "power_supply/battery").mkdir(parents=True)
    (d / "power_supply/battery/type").write_text("Battery\n")
    (d / "power_supply/battery/charge_counter").write_text("4000000\n")
    out = binary("battery",
                 "drain|Charging|4000000|4200000|60000",     # charging: nothing
                 "drain|Discharging|3999000|4000000|60000",  # first discharging sample: baseline
                 "drain|Discharging|3998000|4000000|60000",  # 1000 µAh at 4.0 V = 14400 mJ
                 "drain|Discharging|3998500|4000000|60000",  # counter went up: nothing
                 "drain|Full|3998500|4000000|60000",
                 "drain|Discharging|3998000|3800000|60000",  # after a pause: baseline again
                 ).stdout
    assert kv(out)["battery"] == "power_supply/battery"
    drains = [line.split("=")[1] for line in out.splitlines() if line.startswith("drain=")]
    assert drains == ["0.000 0", "0.000 0", "14400.000 60000", "0.000 0", "0.000 0", "0.000 0"]


def test_usage_file_round_trip_and_prune(energy):
    d, binary = energy
    hour = 490000
    out = binary(f"add|{hour}|app/org.gnome.Maps|1|10.25|1000|gnome-maps",
                 f"add|{hour}|app/org.gnome.Maps|0|5|500|maps-helper",   # smaller: the name stays
                 f"add|{hour}|app/org.gnome.Maps|1|20|100|gnome-maps-2",  # larger: takes over
                 f"add|{hour - 168}|old|1|1|1|",                         # a week old: pruned
                 f"add|{hour - 167}|kept|1|1|1|",
                 f"add|{hour + 2}|future|1|1|1|",                         # clock went back: pruned
                 f"prune|{hour}", "write|usage-out").stdout
    lines = [line for line in out.splitlines() if "\t" in line]
    assert f"{hour}\tapp/org.gnome.Maps\t30.2\t5.0\t1100\t500\tgnome-maps-2" in lines or \
        f"{hour}\tapp/org.gnome.Maps\t30.3\t5.0\t1100\t500\tgnome-maps-2" in lines
    assert any(line.startswith(f"{hour - 167}\tkept\t") for line in lines)
    assert not any("\told\t" in line or "\tfuture\t" in line for line in lines)
    assert out.splitlines()[1].startswith("# plinius-energy 1:")
    # The state file is for the service only
    assert (d / "usage-out").stat().st_mode & 0o777 == 0o600
    # Read back and add once more: values sum up, name kept
    out2 = binary("load|usage-out", f"add|{hour}|app/org.gnome.Maps|1|1|1|", "write|usage-out").stdout
    assert any(line.startswith(f"{hour}\tapp/org.gnome.Maps\t31.") and line.endswith("\tgnome-maps-2")
               for line in out2.splitlines())


def test_usage_file_ignores_broken_lines(energy):
    d, binary = energy
    (d / "usage-out").write_text("# header\n\nonly\ttwo\n490000\tk\t1\t2\t3\t4\n")
    out = binary("load|usage-out", "write|usage-out").stdout
    assert "490000\tk\t1.0\t2.0\t3\t4\t" in out
    assert "only" not in out


def cgroup(d, rel, usage, tids=""):
    p = d / "cgroup" / rel
    p.mkdir(parents=True, exist_ok=True)
    (p / "cpu.stat").write_text(f"usage_usec {usage}\nuser_usec 0\nsystem_usec 0\n")
    (p / "cgroup.threads").write_text(tids)


def proc(d, tid, cmdline, time_in_state):
    p = d / "proc" / str(tid)
    p.mkdir(parents=True, exist_ok=True)
    (p / "cmdline").write_bytes(cmdline)
    (p / "time_in_state").write_text(time_in_state)


def usage_lines(out):
    rows = {}
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) == 7:
            rows[f[1]] = f[2:]
    return rows


MAPS = "user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-org.gnome.Maps-1234.scope"


def test_samples_over_a_cgroup_tree(energy):
    d, binary = energy
    model(d, {0: {500000: 50, 1000000: 100}})
    cgroup(d, "", 1_000_000)
    cgroup(d, "user.slice", 400_000)
    cgroup(d, "user.slice/user-1000.slice", 400_000)
    cgroup(d, "user.slice/user-1000.slice/user@1000.service", 400_000)
    cgroup(d, "user.slice/user-1000.slice/user@1000.service/app.slice", 400_000)
    cgroup(d, MAPS, 400_000, "42\n")
    cgroup(d, "system.slice", 100_000)
    cgroup(d, "system.slice/foo.service", 100_000, "43\n")
    proc(d, 42, b"/usr/bin/gnome-maps\0", "cpu0\n1000000 40\n")   # 400 ms at 100 mW
    proc(d, 43, b"/usr/bin/food\0", "cpu0\n500000 10\n")          # 100 ms at 50 mW
    root = d / "cgroup"
    out = binary(
        "since-boot|1", "sample", "run-file",
        # Second sample: Maps ran 100 ms more; foo.service ended, its last 50 ms are in the
        # parent's counter
        "put|cgroup/cpu.stat|usage_usec 1150000\n",
        "put|cgroup/user.slice/cpu.stat|usage_usec 500000\n",
        "put|cgroup/user.slice/user-1000.slice/cpu.stat|usage_usec 500000\n",
        "put|cgroup/user.slice/user-1000.slice/user@1000.service/cpu.stat|usage_usec 500000\n",
        "put|cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/cpu.stat|usage_usec 500000\n",
        f"put|cgroup/{MAPS}/cpu.stat|usage_usec 500000\n",
        "put|proc/42/time_in_state|cpu0\n1000000 50\n",
        "put|cgroup/system.slice/cpu.stat|usage_usec 150000\n",
        "rmdir|cgroup/system.slice/foo.service",
        "screen|0", "sample", "run-file",
    ).stdout
    first, second = out.split("# plinius-energy 1:")[1:3]
    rows = usage_lines(first)
    assert rows["app/org.gnome.Maps"][:4] == ["40.0", "0.0", "400", "0"]
    assert rows["app/org.gnome.Maps"][4] == "gnome-maps"
    assert rows["system/foo.service"][:4] == ["5.0", "0.0", "100", "0"]
    # The root's own 500 ms (interrupts) have no thread: weighted with everyone's mix,
    # 45000 µJ / 500 ms = 90 mW
    assert rows["kernel"][:4] == ["45.0", "0.0", "500", "0"]
    assert set(rows) == {"app/org.gnome.Maps", "system/foo.service", "kernel"}
    rows = usage_lines(second)
    assert rows["app/org.gnome.Maps"][:4] == ["40.0", "10.0", "400", "100"]
    # The ended service keeps its last moments, at its own last mix (50 mW)
    assert rows["system/foo.service"][:4] == ["5.0", "2.5", "100", "50"]
    assert "system/system.slice" not in rows
    # Screen time is counted only while the screen is on; the second sample was screen off
    assert "@screen" not in rows
    assert (d / "run-usage").stat().st_mode & 0o777 == 0o640
    assert root.exists()


def test_restart_counts_only_what_comes_after(energy):
    d, binary = energy
    model(d, {0: {1000000: 100}})
    cgroup(d, "", 1_000_000)
    cgroup(d, "system.slice", 1_000_000)
    cgroup(d, "system.slice/foo.service", 1_000_000, "43\n")
    proc(d, 43, b"/usr/bin/food\0", "cpu0\n1000000 100\n")
    out = binary("since-boot|0", "sample", "run-file",
                 "put|cgroup/cpu.stat|usage_usec 1100000\n",
                 "put|cgroup/system.slice/cpu.stat|usage_usec 1100000\n",
                 "put|cgroup/system.slice/foo.service/cpu.stat|usage_usec 1100000\n",
                 "put|proc/43/time_in_state|cpu0\n1000000 110\n",
                 "sample", "run-file").stdout
    first, second = out.split("# plinius-energy 1:")[1:3]
    # After a restart of the service the first sample is only the baseline
    assert "foo.service" not in first
    assert usage_lines(second)["system/foo.service"][:4] == ["10.0", "0.0", "100", "0"]


# ---- What is missing or cannot be written ----

def test_thread_without_time_in_state(energy):
    d, binary = energy
    model(d, {0: {1000000: 100}})
    (d / "proc/44").mkdir(parents=True)
    (d / "proc/44/time_in_state").write_text("")
    assert kv(binary("em", "thread|44").stdout)["thread 44"].startswith("0 ")


def test_usage_file_missing(energy):
    d, binary = energy
    out = binary("load|no-such-file", "write|usage-out").stdout
    assert kv(out)["write"] == "1"
    assert usage_lines(out) == {}


def test_usage_file_that_cannot_be_written(energy):
    d, binary = energy
    r = binary("add|490000|k|1|1|1|", "write|no-dir/usage")
    assert kv(r.stdout)["write"] == "0"


def test_mode_that_cannot_be_set_is_reported(energy):
    d, binary = energy
    r = binary("add|490000|k|1|1|1|", "write|usage-out", env={**os.environ, "FAKE_CHMOD_FAILS": "1"})
    # Written all the same; the warning names the file
    assert kv(r.stdout)["write"] == "1"
    assert f"chmod {d}/usage-out: Operation not permitted" in r.stderr


def test_settings_group(built_group):
    d, binary = clean(built_group)
    model(d, {0: {1000000: 100}})
    cgroup(d, "", 1000)
    r = binary("sample")
    st = (d / "run-usage").stat()
    assert st.st_mode & 0o777 == 0o640
    if os.geteuid() == 0:
        assert st.st_gid == os.getgid()
    else:
        # Only root can give the file to root and Settings' group: tried, and said so
        assert f"chown {d}/run-usage: Operation not permitted" in r.stderr


def test_sample_with_gaps_in_the_tree(energy):
    d, binary = energy
    model(d, {0: {1000000: 100}})
    cgroup(d, "", 1_000_000)
    # A cgroup without cpu.stat counts nothing of its own; a thread that ended between
    # cgroup.threads and its time_in_state is skipped
    (d / "cgroup/system.slice").mkdir(parents=True)
    cgroup(d, "system.slice/foo.service", 300_000, "43\n99\n")
    proc(d, 43, b"/usr/bin/food\0", "cpu0\n1000000 30\n")
    out = binary("since-boot|1", "sample", "run-file").stdout
    rows = usage_lines(out)
    assert rows["system/foo.service"][:4] == ["30.0", "0.0", "300", "0"]


def test_thread_id_used_again(energy):
    d, binary = energy
    model(d, {0: {500000: 50, 1000000: 100}})
    for rel in ("", "system.slice"):
        cgroup(d, rel, 500_000)
    cgroup(d, "system.slice/foo.service", 500_000, "43\n")
    proc(d, 43, b"/usr/bin/food\0", "cpu0\n500000 50\n1000000 0\n")
    # The thread ended, a new one got its id and ran 20 ms at the higher speed: counted from 0
    # (100 mW), not as the difference to the old thread's counters
    steps = [f"put|cgroup/{rel}/cpu.stat|usage_usec 520000\n" for rel in ("", "system.slice", "system.slice/foo.service")]
    out = binary("since-boot|1", "sample", *steps, "put|proc/43/time_in_state|cpu0\n500000 0\n1000000 2\n",
                 "sample", "run-file").stdout
    assert usage_lines(out)["system/foo.service"][:4] == ["27.0", "0.0", "520", "0"]


def test_battery_without_voltage_is_not_counted(energy):
    d, binary = energy
    (d / "power_supply/battery").mkdir(parents=True)
    (d / "power_supply/battery/type").write_text("Battery\n")
    (d / "power_supply/battery/charge_counter").write_text("4000000\n")
    out = binary("battery", "drain|Discharging|4000000|4000000|0",
                 "drain|Discharging|3990000|4000000|60000",
                 "rm|power_supply/battery/voltage_now", "drain-again|60000").stdout
    # Without the voltage the energy is unknown: not taken as discharging
    drains = [line.split("=")[1] for line in out.splitlines() if line.startswith("drain=")]
    assert drains == ["0.000 0", "144000.000 60000", "0.000 0"]


def test_run_and_state_files_that_cannot_be_written(energy):
    d, binary = energy
    model(d, {0: {1000000: 100}})
    cgroup(d, "", 1000)
    (d / "run-usage").mkdir()
    (d / "state-usage").mkdir()
    # The hour changed since the last save: the state file is due as well
    r = binary("saved-hour|1", "sample")
    assert f"Writing {d}/run-usage failed" in r.stderr
    assert f"Writing {d}/state-usage failed" in r.stderr


def test_state_file_saved_when_the_hour_changes(energy):
    d, binary = energy
    model(d, {0: {1000000: 100}})
    cgroup(d, "", 1000)
    # Not on the first sample of a run (main saves when it ends), then once per hour
    binary("sample")
    assert not (d / "state-usage").exists()
    binary("saved-hour|1", "sample")
    assert (d / "state-usage").read_text().startswith("# plinius-energy 1:")
    assert (d / "state-usage").stat().st_mode & 0o777 == 0o600


# ---- The daemon ----

MCE = """<node><interface name="com.nokia.mce.request">
  <method name="get_display_status"><arg type="s" direction="out"/></method></interface></node>"""


@pytest.fixture
def bus(tmp_path):
    b = PrivateBus(tmp_path)
    yield b
    b.close()


def mce(bus, state="on"):
    from gi.repository import GLib
    return FakeService(bus, "com.nokia.mce", "/com/nokia/mce/request", MCE,
                       {"get_display_status": lambda p: GLib.Variant("(s)", (state,))})


def display(service, state, sig="(s)"):
    from gi.repository import GLib
    service.emit("/com/nokia/mce/signal", "com.nokia.mce.signal", "display_status_ind",
                 GLib.Variant(sig, (state,)))


def tree(d, ticks, usage, charge):
    cgroup(d, "", usage)
    cgroup(d, "system.slice", usage)
    cgroup(d, "system.slice/foo.service", usage, "43\n")
    proc(d, 43, b"/usr/bin/food\0", f"cpu0\n1000000 {ticks}\n")
    b = d / "power_supply/battery"
    b.mkdir(parents=True, exist_ok=True)
    (b / "type").write_text("Battery\n")
    (b / "status").write_text("Discharging\n")
    (b / "charge_counter").write_text(f"{charge}\n")
    (b / "voltage_now").write_text("4000000\n")


def rows_of(path):
    try:
        return usage_lines(path.read_text())
    except FileNotFoundError:
        return {}


def daemon(binary, bus_address):
    return Daemon([binary.args[0], "main"], sanitized_env(DBUS_SYSTEM_BUS_ADDRESS=bus_address))


def test_daemon_counts_screen_on_and_off(energy, bus):
    d, binary = energy
    model(d, {0: {1000000: 100}})
    tree(d, 100, 1_000_000, 4_000_000)
    service = mce(bus, "on")
    try:
        p = daemon(binary, bus.address)
        # No run file yet (first start since boot): everything since boot counts, as screen on
        rows = poll(lambda: rows_of(d / "run-usage"), what="first sample")
        assert rows["system/foo.service"][:4] == ["100.0", "0.0", "1000", "0"]
        assert service.calls == [("get_display_status", ())]
        # Dimmed still shows something: no change of state
        display(service, "dimmed")
        # 100 ms more while the screen is on, and 1000 µAh at 4 V drained; then it goes off:
        # what came before the change still counts as screen on
        tree(d, 110, 1_100_000, 3_999_000)
        display(service, 7, "(i)")          # not MCE's signal: ignored
        display(service, "off")
        rows = poll(lambda: (r := rows_of(d / "run-usage")).get("system/foo.service", [""])[0] == "110.0"
                    and r, what="sample at the display change")
        assert rows["system/foo.service"][:4] == ["110.0", "0.0", "1100", "0"]
        assert rows["@battery"][0] == "14400.0" and rows["@battery"][1] == "0.0"
        assert float(rows["@screen"][2]) > 0
        # With the screen off the next sample comes from the (shortened) screen-off timer
        tree(d, 120, 1_200_000, 3_998_000)
        rows = poll(lambda: (r := rows_of(d / "run-usage")).get("system/foo.service", ["", ""])[1] == "10.0"
                    and r, what="screen-off sample")
        assert rows["system/foo.service"][:4] == ["110.0", "10.0", "1100", "100"]
        assert rows["@battery"][:2] == ["14400.0", "14400.0"]
        code, err, out = p.stop()
    finally:
        service.close()
    assert code == 0
    # Saved for the next start when it ends, for the service only
    state = d / "state-usage"
    assert usage_lines(state.read_text())["system/foo.service"][:4] == ["110.0", "10.0", "1100", "100"]
    assert state.stat().st_mode & 0o777 == 0o600
    assert (d / "run-usage").stat().st_mode & 0o777 == 0o640


def test_daemon_restarted(energy, bus):
    d, binary = energy
    model(d, {0: {1000000: 100}})
    tree(d, 100, 1_000_000, 4_000_000)
    # A restart of the service: the run file is there and newer than the state file, which an
    # old version left world-readable
    hour = int(__import__("time").time()) // 3600
    (d / "run-usage").write_text(f"# plinius-energy 1:\n{hour}\tapp/x\t5\t1\t50\t10\tx\n")
    (d / "state-usage").write_text(f"# plinius-energy 1:\n{hour}\tapp/old\t1.0\t1.0\t1\t1\told\n")
    (d / "state-usage").chmod(0o644)
    p = daemon(binary, bus.address)      # no MCE on this bus: the screen counts as on
    poll(lambda: (d / "state-usage").stat().st_mode & 0o777 == 0o600, what="state file closed")
    # The first sample rewrites the run file (in its own number format)
    poll(lambda: "\t5.0\t1.0\t" in (d / "run-usage").read_text(), what="first sample")
    code, err, out = p.stop()
    assert code == 0
    rows = usage_lines((d / "state-usage").read_text())
    # What the run file had is kept; the processor time before the restart is not counted again
    assert rows["app/x"][:4] == ["5.0", "1.0", "50", "10"]
    assert "app/old" not in rows
    assert rows.get("system/foo.service", ["0.0"])[0] == "0.0"


def test_daemon_without_a_bus(energy, tmp_path):
    import signal
    d, binary = energy
    model(d, {0: {1000000: 100}})
    tree(d, 100, 1_000_000, 4_000_000)
    p = daemon(binary, f"unix:path={tmp_path}/no-bus")
    p.wait_for("No system bus, counting everything as screen on")
    poll(lambda: rows_of(d / "run-usage"), what="first sample")
    code, err, out = p.stop(signal.SIGINT)
    assert code == 0
    assert usage_lines((d / "state-usage").read_text())["system/foo.service"][0] == "100.0"


def test_daemon_reports_what_it_cannot_write(energy, tmp_path):
    d, binary = energy
    model(d, {0: {1000000: 100}})
    tree(d, 100, 1_000_000, 4_000_000)
    (d / "state-usage").write_text("")
    p = Daemon([binary.args[0], "main"], sanitized_env(DBUS_SYSTEM_BUS_ADDRESS=f"unix:path={tmp_path}/none",
                                                         FAKE_CHMOD_FAILS=1))
    p.wait_for(f"chmod {d}/state-usage: Operation not permitted")
    poll(lambda: rows_of(d / "run-usage"), what="first sample")
    (d / "state-usage").unlink()
    (d / "state-usage").mkdir()
    code, err, out = p.stop()
    assert code == 0
    assert f"Writing {d}/state-usage failed" in err


def test_daemon_without_energy_model(energy):
    d, binary = energy
    r = binary("main")
    assert "No energy model in" in r.stderr
    assert not (d / "run-usage").exists()
