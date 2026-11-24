# The Battery page's own pages (cc-plinius-battery-usage.c): "Battery Usage" from plinius-energy's
# record (its RUN_FILE in the program's root, written by the test in plinius-energy's format:
# hour, key, energy with the screen on and off in mJ, time on and off in ms, process name) and
# "Battery Condition" with the group of cc-plinius-battery.c, which also gives the battery's full
# energy. Apps come from desktop files in the test's XDG_DATA_DIRS. The page refreshes every
# REFRESH_S seconds: 1 s in the test copy instead of 60.

import time

import pytest

from gcc_harness import (Node, wait_for, build_panel, call, click, dump, gcc_session, gcc_world, push, select,  # noqa: F401
                         settle, wait)

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-battery.h"
#include "cc-plinius-battery-usage.h"

static GtkWidget *condition;

/* A battery whose full energy is @wh (UPower's facts the condition group shows) */
static void
battery (double wh)
{
    g_autoptr(UpDevice) device = up_device_new ();

    /* Every fact the group reads, as UPower fills them all (unset ones are warned about) */
    g_object_set (device, "energy-full", wh, "charge-cycles", -1, "capacity", 0.0, "energy-full-design", 0.0,
                  "temperature", 0.0, "voltage", 0.0, "energy-rate", 0.0, "state", 0, "technology", 0, NULL);
    cc_plinius_battery_group_update (condition, device);
}

void
gcc_test_setup (GccTest *t)
{
    condition = cc_plinius_battery_group_new ();
    if (gcc_test_param (t, "full_wh"))
        battery (g_ascii_strtod (gcc_test_param (t, "full_wh"), NULL));
    gcc_test_add (t, cc_plinius_battery_pages_new (t->panel, condition));
}
'''

USAGE = "plinius-battery-usage"
CONDITION = "plinius-battery-condition"


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    reps = [('"/usr/bin/plinius-energy"', '"{root}/usr/bin/plinius-energy"'),
            ('"/run/plinius-energy/usage"', '"{root}/run/plinius-energy/usage"'),
            ("#define REFRESH_S 60", "#define REFRESH_S 1")]
    return build_panel(gcc_session, tmp_path_factory.mktemp("battery-usage"), "battery_usage",
                       ["cc-plinius-battery-usage.c", "cc-plinius-battery.c"], GLUE,
                       replacements={"cc-plinius-battery-usage.c": reps}, pkgs=("upower-glib",))


class Energy:
    """plinius-energy in the program's root: the tool and its record."""

    def __init__(self, root):
        self.root = root
        self.record = root / "run/plinius-energy/usage"

    def install(self):
        tool = self.root / "usr/bin/plinius-energy"
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)

    def write(self, lines, raw=""):
        """lines: (hours ago, key, on_mj, off_mj, on_ms, off_ms, name or None)."""
        now = int(time.time()) // 3600
        out = ["# hour\tkey\ton_mj\toff_mj\ton_ms\toff_ms\tname", ""]
        for ago, key, on_mj, off_mj, on_ms, off_ms, name in lines:
            fields = [str(now - ago), key, str(on_mj), str(off_mj), str(on_ms), str(off_ms)]
            if name is not None:
                fields.append(name)
            out.append("\t".join(fields))
        self.record.parent.mkdir(parents=True, exist_ok=True)
        self.record.write_text("\n".join(out) + "\n" + raw)

    def clear(self):
        import shutil
        for d in list(self.root.iterdir()):
            shutil.rmtree(d) if d.is_dir() else d.unlink()


@pytest.fixture
def energy(program):
    e = Energy(program.root)
    e.clear()
    e.install()
    return e


def desktop(world, app_id, name, exe, extra=""):
    """A desktop file; GIO takes it only when its program is there, so that comes from
    fake_bin (an absolute Exec names the program there)."""
    program = exe.split()[0]
    world.fake_bin.stub(program.rsplit("/", 1)[-1])
    if program.startswith("/"):
        exe = exe.replace(program, str(world.fake_bin.path / program.rsplit("/", 1)[-1]))
    (world.data_dir / "applications" / f"{app_id}.desktop").write_text(
        f"[Desktop Entry]\nType=Application\nName={name}\nExec={exe}\n{extra}")


def summary(screen):
    """{title: (subtitle, value)} of the summary rows that show."""
    out = {}
    for title in ("Nothing Recorded Yet", "Screen On", "Battery Used", "Processor"):
        for row in screen.find(type="AdwActionRow", text=title):
            labels = [c.get("text", "") for c in row.get("children", []) if c["type"] == "GtkLabel"]
            out[title] = (row.get("subtitle"), labels[0] if labels else "")
    return out


def _suffixes(row):
    """The percent label and the bar value among a row's own children."""
    kids = row.get("children", [])
    labels = [c.get("text", "") for c in kids if c["type"] == "GtkLabel"]
    bars = [c["value"] for c in kids if c["type"] == "GtkLevelBar"]
    return labels[0] if labels else None, round(bars[0], 3) if bars else None


def _rows(nodes, out):
    for n in nodes:
        if n["type"] == "AdwExpanderRow":
            # Its suffixes sit in its own header row (an AdwActionRow of the same title)
            header = next(c for c in Node(n).walk()
                          if c["type"] == "AdwActionRow" and c.get("text") == n["text"])
            out.append((n["text"], n.get("subtitle"), *_suffixes(header)))
        elif n["type"] == "AdwActionRow":
            out.append((n.get("text"), n.get("subtitle"), *_suffixes(n)))
        else:
            _rows(n.get("children", []), out)


def share_rows(screen, group):
    """[(title, subtitle, percent, bar)] of a group's rows; None if the group does not show."""
    if not screen.has(type="AdwPreferencesGroup", text=group):
        return None
    out = []
    _rows(screen.one(type="AdwPreferencesGroup", text=group).get("children", []), out)
    return out


def part_rows(expander):
    """[(title, subtitle)] of the rows inside an open expander row."""
    return [(n["text"], n.get("subtitle")) for n in Node(expander).walk()
            if n["type"] == "AdwActionRow" and n.get("text") != expander["text"]]


def test_without_plinius_energy_only_the_condition(program, gcc_world):
    e = Energy(program.root)
    e.clear()
    r = gcc_world.run(program, [dump("main"), push(CONDITION), dump("condition")],
                      params={"full_wh": "16"})
    main = r.dump("main")
    assert main.rows() == [("Battery Condition", "Health, charge cycles, temperature")]
    condition = r.dump("condition")
    assert condition.one(type="AdwNavigationPage", tag=CONDITION)["text"] == "Battery Condition"
    # The page has the title, the group none
    assert condition.has(type="AdwActionRow", text="Full Charge") is False
    assert not condition.has(type="AdwPreferencesGroup", text="Battery Condition")
    assert not r.warnings()


def test_without_battery_no_condition_row(program, gcc_world, energy):
    r = gcc_world.run(program, [dump("main")])
    assert r.dump("main").rows() == [("Battery Usage", "Which apps use the most energy")]


def test_nothing_recorded_yet(program, gcc_world, energy):
    r = gcc_world.run(program, [push(USAGE), dump("usage")])
    s = r.dump("usage")
    assert s.one(type="AdwNavigationPage", tag=USAGE)["text"] == "Battery Usage"
    assert summary(s) == {"Nothing Recorded Yet": (
        "The record starts with the phone, usage shows here within a minute.", "")}
    assert share_rows(s, "Apps") is None and share_rows(s, "System") is None
    toggles = s.one(type="AdwToggleGroup")
    assert toggles["toggles"] == ["Last 24 Hours", "Last 7 Days"] and toggles["active_name"] == "day"


def test_only_screen_time_is_nothing_recorded(program, gcc_world, energy):
    energy.write([(0, "@screen", 0, 0, 60000, 0, None),
                  # Energy without time and time without energy: no app has used anything
                  (0, "system/idle.service", 0, 0, 5000, 0, "idle")])
    r = gcc_world.run(program, [push(USAGE), dump("usage")])
    assert list(summary(r.dump("usage"))) == ["Nothing Recorded Yet"]


def test_usage_as_share_of_a_charge(program, gcc_world, energy):
    desktop(gcc_world, "org.example.Maps", "Maps", "maps")
    energy.write([
        (0, "@screen", 0, 0, 3723000, 0, None),
        (1, "@battery", 1800000, 1800000, 3000000, 600000, None),
        (0, "app/org.example.Maps", 2000000, 0, 120000, 0, "maps"),
        (2, "system/NetworkManager.service", 600000, 400000, 30000, 30000, "NetworkManager"),
        # Under a second of processor time: no row
        (0, "kernel", 500, 0, 500, 0, None),
    ])
    r = gcc_world.run(program, [push(USAGE), dump("usage")], params={"full_wh": "10"})
    s = r.dump("usage")
    assert summary(s) == {
        "Screen On": (None, "1 h 2 min"),
        "Battery Used": ("Measured over 1 h 0 min unplugged", "10 % of a charge"),
        "Processor": ("Estimated, all apps and services together", "8.3 % of a charge"),
    }
    assert share_rows(s, "Apps") == [("Maps", "2 min", "67 %", 1.0)]
    assert share_rows(s, "System") == [
        ("Network", "NetworkManager · 1 min, 30 s with the screen off", "33 %", 0.5)]
    assert s.one(type="AdwPreferencesGroup", text="Apps")["subtitle"] == "Share of the processor's energy"
    assert not r.warnings()


def test_energy_in_mah_without_the_battery_s_energy(program, gcc_world, energy):
    energy.write([
        (0, "@battery", 100000, 0, 30000, 0, None),
        (0, "system/a.service", 13000000, 860000, 0, 45000, "a"),
    ])
    r = gcc_world.run(program, [push(USAGE), dump("usage")])
    s = r.dump("usage")
    assert summary(s) == {
        "Screen On": (None, "0 s"),
        # Less than a minute unplugged is not worth saying
        "Battery Used": ("Not unplugged in this time", ""),
        "Processor": ("Estimated, all apps and services together", "1000 mAh"),
    }
    assert share_rows(s, "System") == [("a", "45 s with the screen off", "100 %", 1.0)]
    assert share_rows(s, "Apps") is None


def test_what_each_record_key_is_called(program, gcc_world, energy):
    desktop(gcc_world, "org.example.Calls", "Calls", "/opt/callsd")
    desktop(gcc_world, "org.example.Hidden", "Hidden Tool", "hiddenbin", "NoDisplay=true\n")
    desktop(gcc_world, "org.example.Wrapper", "Wrapper", "wrapped", "NoDisplay=true\n")
    desktop(gcc_world, "org.example.Wrapped", "Wrapped App", "wrapped")
    desktop(gcc_world, "org.example.Flatpak", "Flat", "flatpak run org.example.Flatpak")
    s1 = 1000
    energy.write([
        # The desktop file of the key, or of the program when that one does not show
        (0, "app/org.example.Wrapper", 900, 0, s1, 0, "wrapped"),
        # One that does not show, without a program that does: its name, but not as an app
        (0, "app/org.example.Hidden", 800, 0, s1, 0, "other"),
        # No desktop file: the process name, or the key's unit
        (0, "app/org.example.Gone", 700, 0, s1, 0, "gonebin"),
        (0, "app/org.example.Nameless", 600, 0, s1, 0, ""),
        # A service of an app (by its program)
        (0, "user/calls-daemon.service", 1100, 0, s1, 0, "callsd"),
        # A launcher is no app's program; a hidden app's program neither
        (0, "user/runner.service", 500, 0, s1, 0, "flatpak"),
        (0, "user/h.service", 480, 0, s1, 0, "hiddenbin"),
        # What ended inside a slice, the kernel, and processes better known by what they do
        (0, "user/app.slice", 400, 0, s1, 0, None),
        (0, "system/system.slice", 50, 0, s1, 0, None),
        (0, "kernel", 300, 0, s1, 0, None),
        (0, "user/pipewire.service", 200, 0, s1, 0, "pipewire"),
        (0, "user/wireplumber.service", 150, 0, s1, 0, "wireplumber"),
        # Without a name: the unit without its type, or all of it
        (0, "system/foo-bar.service", 120, 0, s1, 0, None),
        (0, "system/noext", 110, 0, s1, 0, None),
        (0, "system/mine.service", 100, 0, s1, 0, "<b>mydaemon</b>"),
    ])
    r = gcc_world.run(program, [push(USAGE), dump("usage")])
    s = r.dump("usage")
    apps = [(t, sub) for t, sub, _p, _b in share_rows(s, "Apps")]
    system = [(t, sub) for t, sub, _p, _b in share_rows(s, "System")]
    assert apps == [("Calls", "1 s"), ("Wrapped App", "1 s")]
    assert system == [
        ("Hidden Tool", "1 s"),
        ("gonebin", "1 s"),
        ("org.example.Nameless", "1 s"),
        ("flatpak", "1 s"),
        ("hiddenbin", "1 s"),
        ("Ended Programs", "2 s"),
        ("Sound", "pipewire · 2 s"),
        ("Linux Kernel", "1 s"),
        ("foo-bar", "1 s"),
        ("noext", "1 s"),
        # Shown as it is named, not as markup
        ("<b>mydaemon</b>", "1 s"),
    ]


def test_android_services_in_one_row_with_their_processes(program, gcc_world, energy):
    lines = [(0, f"android/proc{i:02d}", 1000 + i * 100, 0, 1000, 0, None) for i in range(12)]
    lines.append((0, "android/proc11", 0, 500, 0, 2000, None))
    energy.write(lines)
    r = gcc_world.run(program, [push(USAGE), dump("closed"),
                                click(type="AdwExpanderRow", text="Android Services"), settle(),
                                dump("open")])
    closed = share_rows(r.dump("closed"), "System")
    assert closed == [("Android Services", "14 s, 2 s with the screen off", "100 %", 1.0)]
    expander = r.dump("open").one(type="AdwExpanderRow", text="Android Services")
    parts = part_rows(expander)
    # The ten largest, the largest first
    assert parts == [("proc11", "3 s, 2 s with the screen off")] + [
        (f"proc{i:02d}", "1 s") for i in range(10, 1, -1)]


def test_at_most_twenty_system_rows_largest_first(program, gcc_world, energy):
    energy.write([(0, f"system/s{i:02d}.service", 1000 + i, 0, 2000, 0, f"s{i:02d}")
                  for i in range(25)])
    r = gcc_world.run(program, [push(USAGE), dump("usage")])
    titles = [t for t, *_ in share_rows(r.dump("usage"), "System")]
    assert titles == [f"s{i:02d}" for i in range(24, 4, -1)]


def test_small_shares_and_durations(program, gcc_world, energy):
    energy.write([
        (0, "system/big.service", 99950, 0, 3600000 * 2 + 60000 * 5, 0, "big"),
        (0, "system/mid.service", 0, 5, 59400, 0, "mid"),
        (0, "system/tiny.service", 45, 0, 1000, 0, "tiny"),
    ])
    r = gcc_world.run(program, [push(USAGE), dump("usage")])
    assert share_rows(r.dump("usage"), "System") == [
        ("big", "2 h 5 min", "100 %", 1.0),
        ("tiny", "1 s", "< 0.1 %", 0.0),
        ("mid", "59 s", "< 0.1 %", 0.0),
    ]


def test_seven_days_count_what_the_last_day_leaves_out(program, gcc_world, energy):
    energy.write([
        (0, "system/today.service", 1000, 0, 1000, 0, "today"),
        (30, "system/yesterday.service", 3000, 0, 1000, 0, "yesterday"),
        # The 8th day back is out of both
        (7 * 24, "system/old.service", 9000, 0, 1000, 0, "old"),
    ], raw="garbage line\n1\t2\t3\n")
    r = gcc_world.run(program, [push(USAGE), dump("day"),
                                select(name="week", type="AdwToggleGroup"), settle(), dump("week"),
                                select(name="day", type="AdwToggleGroup"), settle(), dump("again")])
    assert [t for t, *_ in share_rows(r.dump("day"), "System")] == ["today"]
    assert [(t, p) for t, _s, p, _b in share_rows(r.dump("week"), "System")] == [
        ("yesterday", "75 %"), ("today", "25 %")]
    # The rows of before are gone, not doubled
    assert [t for t, *_ in share_rows(r.dump("again"), "System")] == ["today"]


def test_the_page_refreshes_while_it_shows(program, gcc_world, energy):
    energy.write([(0, "system/a.service", 1000, 0, 1000, 0, "first")])
    later = Energy(program.root)
    now = int(time.time()) // 3600
    text = f"{now}\tsystem/b.service\t1000\t0\t2000\t0\tsecond\n"
    r = gcc_world.run(program, [
        push(USAGE), dump("before"),
        {"op": "write_file", "path": str(later.record), "text": text},
        # The refresh comes after REFRESH_S (1 s in the test copy); under load later than that
        wait_for(timeout=10000, text="second"), dump("after")])
    assert [t for t, *_ in share_rows(r.dump("before"), "System")] == ["first"]
    assert share_rows(r.dump("after"), "System") == [("second", "2 s", "100 %", 1.0)]


def test_apps_installed_meanwhile_are_found_the_next_time(program, gcc_world, energy):
    energy.write([(0, "user/newd.service", 1000, 0, 1000, 0, "newd")])
    gcc_world.fake_bin.stub("newd")
    entry = gcc_world.data_dir / "applications" / "org.example.New.desktop"
    r = gcc_world.run(program, [
        push(USAGE), dump("before"),
        {"op": "write_file", "path": str(entry),
         "text": "[Desktop Entry]\nType=Application\nName=New App\nExec=newd\n"},
        settle(300), back_step(), settle(), push(USAGE), dump("after")])
    assert [t for t, *_ in share_rows(r.dump("before"), "System")] == ["newd"]
    assert [t for t, *_ in share_rows(r.dump("after"), "Apps")] == ["New App"]


def back_step():
    return {"op": "back"}


def test_rows_open_the_pages(program, gcc_world, energy):
    r = gcc_world.run(program, [click(type="CcListRow", text="Battery Usage"), settle(),
                                dump("usage"), back_step(), settle(),
                                click(type="CcListRow", text="Battery Condition"), settle(),
                                dump("condition")], params={"full_wh": "12.5"})
    assert r.dump("usage").one(type="AdwNavigationPage", tag=USAGE)
    assert r.dump("condition").one(type="AdwNavigationPage", tag=CONDITION)
