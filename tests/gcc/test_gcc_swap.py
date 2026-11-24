# System > Memory and Swap (cc-plinius-swap.c): what the page shows from /proc/meminfo,
# /proc/swaps, /proc/sys/vm/swappiness, zram's mm_stat, /proc/<pid>/status and
# /etc/plinius-swap.conf (all in the program's root), and the changes it makes through
# "pkexec plinius-swap-set ..." (a fake tool in the program's root that logs its arguments, answers
# with an exit code and, like the real one, changes what the page reads).

import shutil

import pytest

from gcc_harness import (back, build_panel, call, click, dump, gcc_session, gcc_world, push,  # noqa: F401
                         set_active, settle, wait, wait_for)

SET_TOOL = "/usr/lib/plinius-swap/plinius-swap-set"
CONF = "/etc/plinius-swap.conf"
ZRAM_STAT = "/sys/block/zram0/mm_stat"
SWAP_FILE = "/home/.plinius-swap/swapfile"
PAGE = {"type": "AdwNavigationPage", "tag": "plinius-swap"}

GLUE = r'''
#include "gcc_driver.h"
#include "cc-list-row.h"
#include "cc-plinius-swap.h"

static gboolean added;

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *group = adw_preferences_group_new ();
    /* The System page's row as its .blp has it, hidden until our file sets it up */
    GtkWidget *row = g_object_new (CC_TYPE_LIST_ROW, "show-arrow", TRUE, "action-name", "navigation.push",
                                   "visible", FALSE, NULL);

    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), row);
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (group));
    added = cc_plinius_swap_add_page (t->panel);
    if (added)
        cc_plinius_swap_row_setup (ADW_ACTION_ROW (row));
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (!g_str_equal (name, "added"))
        return FALSE;
    json_builder_add_boolean_value (result, added);
    return TRUE;
}
'''


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    reps = [('"/proc', '"{root}/proc'), (f'"{CONF}"', f'"{{root}}{CONF}"'), (f'"{SET_TOOL}"', f'"{{root}}{SET_TOOL}"'),
            (f'"{ZRAM_STAT}"', f'"{{root}}{ZRAM_STAT}"'), ('"/home"', '"{root}/home"')]
    return build_panel(gcc_session, tmp_path_factory.mktemp("swap"), "swap", ["cc-plinius-swap.c"], GLUE,
                       replacements={"cc-plinius-swap.c": reps},
                       # The swap file's name is only compared with the names in /proc/swaps
                       allow=("/home/.plinius-swap/",))


KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB


def size(n):
    """g_format_size_full (n, G_FORMAT_SIZE_IEC_UNITS)"""
    if n < 1024:
        return f"{n} bytes" if n != 1 else "1 byte"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        n /= 1024
        if n < 1024 or unit == "TiB":
            return f"{n:.1f}\u00a0{unit}"


class Phone:
    def __init__(self, program, world):
        self.root = program.root
        for d in list(self.root.iterdir()):
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        self.world = world
        self.log = world.tmp / "tool-args"
        self.code = world.tmp / "tool-code"
        self.tool = self.path(SET_TOOL)
        self.tool.parent.mkdir(parents=True)
        # Like plinius-swap-set: what it changes shows in the files the page reads
        self.tool.write_text(f"""#!/bin/bash
echo "$*" >> "{self.log}"
code=$(cat "{self.code}" 2>/dev/null || echo 0)
[ -f "{world.tmp}/tool-wait" ] && sleep "$(cat "{world.tmp}/tool-wait")"
[ "$code" = 0 ] || exit $code
case "$1" in
enable) echo "{SWAP_FILE} file 8388604 0 -2" >> "{self.path('/proc/swaps')}" ;;
disable) sed -i '/swapfile/d' "{self.path('/proc/swaps')}" ;;
size) echo "size_gib=$2" > "{self.path(CONF)}" ;;
swappiness) echo "$2" > "{self.path('/proc/sys/vm/swappiness')}" ;;
drain) sed -i 's/^\\(\\S*\\s*\\S*\\s*[0-9]*\\)\\s*[0-9]*/\\1 0/' "{self.path('/proc/swaps')}" ;;
esac
""")
        self.tool.chmod(0o755)
        world.fake_bin.stub("pkexec", 'exec "$@"')
        self.path("/home").mkdir()
        self.meminfo()
        self.swaps()
        self.swappiness(1)

    def path(self, p):
        return self.root / p.lstrip("/")

    def write(self, p, text):
        f = self.path(p)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        return f

    def meminfo(self, total=4 * GIB, available=3 * GIB, free=GIB, cached=512 * MIB, buffers=0):
        self.write("/proc/meminfo", f"MemTotal: {total // KIB} kB\nMemFree: {free // KIB} kB\n"
                                    f"MemAvailable: {available // KIB} kB\nBuffers: {buffers // KIB} kB\n"
                                    f"Cached: {cached // KIB} kB\nSwapCached: 0 kB\n")

    def swaps(self, *areas):
        """areas: (name, size, used) in bytes"""
        lines = ["Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority"]
        for name, sz, used in areas:
            kind = "file" if "swapfile" in name else "partition"
            lines.append(f"{name}\t{kind}\t{sz // KIB}\t{used // KIB}\t-2")
        self.write("/proc/swaps", "\n".join(lines) + "\n")

    def swappiness(self, value):
        self.write("/proc/sys/vm/swappiness", f"{value}\n")

    def process(self, pid, name, rss=0, swap=0, raw=None):
        self.write(f"/proc/{pid}/status", raw if raw is not None else
                   f"Name:\t{name}\nUmask:\t0022\nState:\tS (sleeping)\nVmRSS:\t{rss // KIB} kB\n"
                   f"VmSwap:\t{swap // KIB} kB\n")

    def args(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def exit_with(self, code):
        self.code.write_text(str(code))


@pytest.fixture
def phone(program, gcc_world):
    return Phone(program, gcc_world)


def rows(screen):
    return dict((t, s) for t, s in screen.within(**PAGE).rows())


def bar(screen, title):
    row = screen.within(**PAGE).one(type="AdwActionRow", text=title)
    return [n for n in row.walk() if n.get("type") == "GtkLevelBar"][0]["value"]


def visible(screen, title):
    return screen.within(**PAGE).has(text=title, type=("AdwActionRow", "AdwSwitchRow", "AdwSpinRow"))


def spin(screen, title):
    return screen.within(**PAGE).one(type="AdwSpinRow", text=title)


SHOW = [push("plinius-swap"), wait_for(**PAGE)]


def test_no_page_without_plinius_swap(program, gcc_world, phone):
    phone.tool.chmod(0o644)
    r = gcc_world.run(program, [call("added"), dump("system")])
    assert r.value("added") is False
    assert not r.dump("system").has(type="CcListRow")


def test_row_on_the_system_page_opens_the_page(program, gcc_world, phone):
    r = gcc_world.run(program, [call("added"), dump("system"), click(type="AdwActionRow", text="Memory and Swap"),
                                wait_for(**PAGE), dump("page")])
    assert r.value("added") is True
    row = r.dump("system").one(type="CcListRow", text="Memory and Swap")
    assert row["subtitle"] == "What RAM and swap hold, swap file and threshold"
    assert row["action"] == "navigation.push" and row["target"] == "'plinius-swap'"
    page = r.dump("page").one(**PAGE)
    assert page["text"] == "Memory and Swap"
    assert [g.get("text") for g in r.dump("page").within(**PAGE).find(type="AdwPreferencesGroup")] == [
        "Settings", "Memory", "Swap", "Largest Processes"]
    assert not r.warnings()


def test_what_ram_and_swap_hold(program, gcc_world, phone):
    phone.meminfo(total=4 * GIB, available=3 * GIB, free=GIB, cached=256 * MIB, buffers=256 * MIB)
    phone.swaps(("/dev/block/zram0", 2 * GIB, 512 * MIB), (SWAP_FILE, 8 * GIB, 2 * GIB),
                ("/dev/sdz9", GIB, 0))
    phone.write(ZRAM_STAT, f"{512 * MIB} {128 * MIB} {160 * MIB} 0 0 0 0 0\n")
    r = gcc_world.run(program, [*SHOW, dump("page")])
    s = r.dump("page")
    got = rows(s)
    assert got["RAM"] == "1.0\u00a0GiB of 4.0\u00a0GiB in use"
    assert got["Cache, given back when needed"] == "512.0\u00a0MiB"
    assert got["Free"] == "1.0\u00a0GiB"
    assert got["Compressed in RAM (zram)"] == "512.0\u00a0MiB of 2.0\u00a0GiB, takes 160.0\u00a0MiB of RAM"
    assert got["Swap File"] == "2.0\u00a0GiB of 8.0\u00a0GiB"
    # 2.5 GiB in swap, room for 3 GiB - 4 GiB / 20
    assert got["Move Swap to RAM"] == "Brings 2.5\u00a0GiB back into RAM"
    assert bar(s, "RAM") == pytest.approx(0.25)
    assert bar(s, "Compressed in RAM (zram)") == pytest.approx(0.25)
    row = s.within(**PAGE).one(type="AdwActionRow", text="RAM")
    assert "property" in row["classes"]
    assert s.within(**PAGE).one(type="GtkButton", text="Move").get("sensitive") is not False
    assert s.within(**PAGE).one(type="AdwSwitchRow", text="Swap File")["active"] is True
    assert not r.warnings()


def test_nothing_in_swap_and_no_zram(program, gcc_world, phone):
    phone.swaps()
    r = gcc_world.run(program, [*SHOW, dump("page")])
    s = r.dump("page")
    got = rows(s)
    assert "Compressed in RAM (zram)" not in got
    # Only the settings' switch of that name
    assert s.within(**PAGE).find(text="Swap File", type="AdwActionRow") == []
    assert got["Move Swap to RAM"] == "Swap is empty"
    assert s.within(**PAGE).one(type="GtkButton", text="Move").get("sensitive") is False
    assert s.within(**PAGE).one(type="AdwSwitchRow", text="Swap File")["active"] is False


def test_zram_without_mm_stat_and_too_much_in_swap(program, gcc_world, phone):
    phone.meminfo(total=4 * GIB, available=GIB, free=256 * MIB, cached=0)
    phone.swaps(("/dev/block/zram0", 2 * GIB, 2 * GIB))
    r = gcc_world.run(program, [*SHOW, dump("page")])
    got = rows(r.dump("page"))
    assert got["Compressed in RAM (zram)"] == "2.0\u00a0GiB of 2.0\u00a0GiB"
    # Room: 1 GiB less a twentieth of 4 GiB
    assert got["Move Swap to RAM"] == f"Not enough free memory: 2.0\u00a0GiB in swap, room for {size(GIB - 4 * GIB // 20)}"
    assert r.dump("page").within(**PAGE).one(type="GtkButton", text="Move").get("sensitive") is False


def test_without_meminfo_and_swaps(program, gcc_world, phone):
    phone.path("/proc/meminfo").unlink()
    phone.path("/proc/swaps").unlink()
    phone.path("/proc/sys/vm/swappiness").unlink()
    r = gcc_world.run(program, [*SHOW, dump("page")])
    got = rows(r.dump("page"))
    assert got["RAM"] == "0 bytes of 0 bytes in use"
    assert bar(r.dump("page"), "RAM") == 0
    assert got["Move Swap to RAM"] == "Swap is empty"
    # No file, no kernel value: the defaults
    assert spin(r.dump("page"), "Size")["value"] == 8
    assert spin(r.dump("page"), "Threshold (Swappiness)")["value"] == 1


def test_settings_from_the_config_and_the_kernel(program, gcc_world, phone):
    phone.write(CONF, "# comment\nenabled=yes\nsize_gib = 12\nswappiness=30\nbroken\n")
    phone.swappiness(60)
    r = gcc_world.run(program, [*SHOW, dump("page")])
    s = r.dump("page")
    assert spin(s, "Size")["value"] == 12
    assert spin(s, "Size")["subtitle"].startswith("In GiB, ") and spin(s, "Size")["subtitle"].endswith(
        " free on the storage")
    assert spin(s, "Threshold (Swappiness)")["value"] == 60
    assert spin(s, "Threshold (Swappiness)")["subtitle"] == (
        "The lower, the later memory moves to swap. 1: only when memory runs short, 60: Linux default, "
        "100: Android default")
    # The size is what applies: no Apply
    assert not s.within(**PAGE).has(type="GtkButton", text="Apply")


@pytest.mark.parametrize("conf, kernel, size_gib, swappiness", [
    ("size_gib=100\n", "0", 64, 1),
    ("size_gib=0\n", "500", 1, 200),
    ("swappiness=25\n", None, 8, 25),
])
def test_values_are_kept_inside_the_rows(program, gcc_world, phone, conf, kernel, size_gib, swappiness):
    phone.write(CONF, conf)
    if kernel is None:
        phone.path("/proc/sys/vm/swappiness").unlink()
    else:
        phone.swappiness(kernel)
    r = gcc_world.run(program, [*SHOW, dump("page")])
    assert spin(r.dump("page"), "Size")["value"] == size_gib
    assert spin(r.dump("page"), "Threshold (Swappiness)")["value"] == swappiness


def test_without_the_home_filesystem_no_free_storage(program, gcc_world, phone):
    phone.path("/home").rmdir()
    r = gcc_world.run(program, [*SHOW, dump("page")])
    assert spin(r.dump("page"), "Size").get("subtitle") is None


def test_largest_processes(program, gcc_world, phone):
    for pid, (name, rss, swap) in enumerate([("firefox", 900 * MIB, 100 * MIB), ("phosh", 200 * MIB, 0),
                                             ("<b>evil</b>", 0, 300 * MIB), ("gnome-calls", 150 * MIB, 30 * MIB),
                                             ("chatty", 100 * MIB, 0), ("feedbackd", 10 * MIB, 0),
                                             ("small", 5 * MIB, 0)], start=100):
        phone.process(pid, name, rss, swap)
    phone.process(200, "kthreadd")                      # no memory of its own
    phone.process(201, "", raw="Umask:\t0022\nVmRSS:\t100 kB\n")  # no Name: first
    phone.process(202, "", raw="VmRSS:\t999999 kB\nName:\tlate\n")
    phone.write("/proc/self-ish/status", "Name:\tnot a pid\nVmRSS:\t9999999 kB\n")
    phone.path("/proc/300").mkdir()                     # gone by now
    r = gcc_world.run(program, [*SHOW, dump("page")])
    group = r.dump("page").within(type="AdwPreferencesGroup", text="Largest Processes")
    assert group.rows() == [("firefox", "RAM 900.0\u00a0MiB, swap 100.0\u00a0MiB"),
                            ("<b>evil</b>", "RAM 0 bytes, swap 300.0\u00a0MiB"),
                            ("phosh", "RAM 200.0\u00a0MiB, swap 0 bytes"),
                            ("gnome-calls", "RAM 150.0\u00a0MiB, swap 30.0\u00a0MiB"),
                            ("chatty", "RAM 100.0\u00a0MiB, swap 0 bytes"),
                            ("feedbackd", "RAM 10.0\u00a0MiB, swap 0 bytes")]


def test_few_processes_leave_rows_hidden(program, gcc_world, phone):
    phone.process(100, "only", 64 * MIB, 0)
    r = gcc_world.run(program, [*SHOW, dump("page")])
    group = r.dump("page").within(type="AdwPreferencesGroup", text="Largest Processes")
    assert group.rows() == [("only", "RAM 64.0\u00a0MiB, swap 0 bytes")]


def test_numbers_follow_while_the_page_shows(program, gcc_world, phone):
    phone.meminfo(total=4 * GIB, available=3 * GIB)
    r = gcc_world.run(program, [*SHOW, dump("before"),
                                {"op": "write_file", "path": str(phone.path("/proc/meminfo")),
                                 "text": f"MemTotal: {4 * GIB // KIB} kB\nMemAvailable: {GIB // KIB} kB\n"},
                                wait(4500), dump("after"), {"op": "back"}, wait(100), dump("back")])
    assert rows(r.dump("before"))["RAM"] == "1.0\u00a0GiB of 4.0\u00a0GiB in use"
    assert rows(r.dump("after"))["RAM"] == "3.0\u00a0GiB of 4.0\u00a0GiB in use"
    assert not r.dump("back").has(**PAGE)


# ----- Changes -----

def test_swap_file_switch_runs_the_tool(program, gcc_world, phone):
    r = gcc_world.run(program, [*SHOW, set_active(True, type="AdwSwitchRow", text="Swap File"), wait(500),
                                dump("on"), set_active(False, type="AdwSwitchRow", text="Swap File"), wait(500),
                                dump("off")])
    assert phone.args() == ["enable", "disable"]
    assert r.dump("on").within(**PAGE).one(type="AdwSwitchRow", text="Swap File")["active"] is True
    on = r.dump("on").within(**PAGE)
    assert on.one(type="AdwSwitchRow", text="Swap File")["subtitle"] == (
        "On the storage, used once the compressed RAM is full")
    assert on.one(type="AdwActionRow", text="Swap File")["subtitle"] == "0 bytes of 8.0\u00a0GiB"
    assert r.dump("off").within(**PAGE).one(type="AdwSwitchRow", text="Swap File")["active"] is False
    assert not r.warnings()


def test_while_the_tool_runs_the_settings_wait(program, gcc_world, phone):
    (gcc_world.tmp / "tool-wait").write_text("1")
    r = gcc_world.run(program, [*SHOW, set_active(True, type="AdwSwitchRow", text="Swap File"), wait(200),
                                dump("busy"), wait(1500), dump("done")])
    group = r.dump("busy").within(**PAGE).one(type="AdwPreferencesGroup", text="Settings")
    assert group.get("sensitive") is False
    assert r.dump("done").within(**PAGE).one(type="AdwPreferencesGroup", text="Settings").get("sensitive") is not False
    assert phone.args() == ["enable"]


def test_size_apply_shows_when_it_would_change_something(program, gcc_world, phone):
    phone.write(CONF, "size_gib=8\n")
    r = gcc_world.run(program, [*SHOW, {"op": "set_value", "target": {"type": "AdwSpinRow", "text": "Size"},
                                        "value": 16}, dump("changed"),
                                click(type="GtkButton", text="Apply"), wait(600), dump("applied")])
    assert r.dump("changed").within(**PAGE).has(type="GtkButton", text="Apply")
    assert "suggested-action" in r.dump("changed").within(**PAGE).one(type="GtkButton", text="Apply")["classes"]
    assert phone.args() == ["size 16"]
    assert spin(r.dump("applied"), "Size")["value"] == 16
    assert not r.dump("applied").within(**PAGE).has(type="GtkButton", text="Apply")


def test_swappiness_goes_to_the_tool_once_stepping_stops(program, gcc_world, phone):
    steps = [{"op": "set_value", "target": {"type": "AdwSpinRow", "text": "Threshold (Swappiness)"}, "value": v}
             for v in (2, 3, 10)]
    r = gcc_world.run(program, [*SHOW, steps[0], wait(300), steps[1], wait(300), steps[2], wait(400),
                                call("added"), wait(900), dump("after")])
    assert phone.args() == ["swappiness 10"]
    assert spin(r.dump("after"), "Threshold (Swappiness)")["value"] == 10


def step_then_switch(program, gcc_world, phone):
    """The threshold stepped, then (before it went to the tool) the switch flipped"""
    (gcc_world.tmp / "tool-wait").write_text("1.5")
    return gcc_world.run(program, [*SHOW, {"op": "set_value", "target": {"type": "AdwSpinRow",
                                                                         "text": "Threshold (Swappiness)"}, "value": 7},
                                   set_active(True, type="AdwSwitchRow", text="Swap File"), wait(3500), dump("after")])


def test_swappiness_waits_for_a_running_call(program, gcc_world, phone):
    # The threshold goes to the tool only after the switch's call
    step_then_switch(program, gcc_world, phone)
    assert phone.args()[0] == "enable" and len(phone.args()) == 2
    assert phone.args()[1].startswith("swappiness ")


def test_swappiness_stepped_before_another_call_is_not_lost(program, gcc_world, phone):
    r = step_then_switch(program, gcc_world, phone)
    assert phone.args() == ["enable", "swappiness 7"]
    assert spin(r.dump("after"), "Threshold (Swappiness)")["value"] == 7


def size_then_switch(gcc_world):
    """The size changed (16 instead of the 8 of the file), not applied, then the switch flipped"""
    gcc_world.tmp.joinpath("tool-wait").write_text("0.3")
    return [*SHOW, {"op": "set_value", "target": {"type": "AdwSpinRow", "text": "Size"}, "value": 16},
            set_active(True, type="AdwSwitchRow", text="Swap File"), wait(900), dump("after")]


def test_a_size_not_applied_stays_after_another_call(program, gcc_world, phone):
    phone.write(CONF, "size_gib=8\n")
    r = gcc_world.run(program, size_then_switch(gcc_world))
    assert phone.args() == ["enable"]
    assert spin(r.dump("after"), "Size")["value"] == 16
    assert r.dump("after").within(**PAGE).has(type="GtkButton", text="Apply")


def test_a_size_not_applied_is_dropped_when_the_page_is_left(program, gcc_world, phone):
    phone.write(CONF, "size_gib=8\n")
    r = gcc_world.run(program, [*size_then_switch(gcc_world), back(), settle(), *SHOW, dump("again")])
    assert spin(r.dump("again"), "Size")["value"] == 8
    assert not r.dump("again").within(**PAGE).has(type="GtkButton", text="Apply")


def test_a_failed_size_goes_back_to_the_file(program, gcc_world, phone):
    phone.write(CONF, "size_gib=8\n")
    phone.exit_with(4)
    r = gcc_world.run(program, [*SHOW, {"op": "set_value", "target": {"type": "AdwSpinRow", "text": "Size"},
                                        "value": 16}, click(type="GtkButton", text="Apply"), wait(600),
                                dump("after")])
    assert phone.args() == ["size 16"]
    assert spin(r.dump("after"), "Size")["value"] == 8
    assert not r.dump("after").within(**PAGE).has(type="GtkButton", text="Apply")


def test_a_threshold_stepped_again_during_its_call_is_sent_next(program, gcc_world, phone):
    (gcc_world.tmp / "tool-wait").write_text("1.0")
    step = lambda v: {"op": "set_value", "target": {"type": "AdwSpinRow", "text": "Threshold (Swappiness)"},
                      "value": v}
    r = gcc_world.run(program, [*SHOW, step(5), wait(1000), step(9), wait(3000), dump("after")])
    assert phone.args() == ["swappiness 5", "swappiness 9"]
    assert spin(r.dump("after"), "Threshold (Swappiness)")["value"] == 9


def test_move_swap_to_ram(program, gcc_world, phone):
    phone.swaps(("/dev/block/zram0", 2 * GIB, 512 * MIB))
    (gcc_world.tmp / "tool-wait").write_text("0.8")
    r = gcc_world.run(program, [*SHOW, click(type="GtkButton", text="Move"), wait(200), dump("moving"), wait(1500),
                                dump("done")])
    moving = r.dump("moving")
    assert rows(moving)["Move Swap to RAM"] == "Moving, this can take a while"
    assert moving.within(**PAGE).one(type="GtkButton", text="Move").get("sensitive") is False
    assert moving.within(**PAGE).has(type="AdwSpinner")
    assert phone.args() == ["drain"]
    assert rows(r.dump("done"))["Move Swap to RAM"] == "Swap is empty"
    assert not r.dump("done").within(**PAGE).has(type="AdwSpinner")


@pytest.mark.parametrize("code, toast", [
    (126, None),
    (127, None),
    (3, "Not enough free memory for what is in swap. Close some apps and try again."),
    (4, "Not enough free storage for a swap file of this size."),
    (5, "No swap file possible: it would be outside the encryption or on an image file."),
    (1, "Could not change swap."),
])
def test_refused_or_failed_change_goes_back_to_what_is_true(program, gcc_world, phone, code, toast):
    phone.exit_with(code)
    r = gcc_world.run(program, [*SHOW, set_active(True, type="AdwSwitchRow", text="Swap File"), wait(600),
                                dump("after")])
    s = r.dump("after")
    assert s.within(**PAGE).one(type="AdwSwitchRow", text="Swap File")["active"] is False
    labels = [n["text"] for n in s.all() if n.get("type") == "GtkLabel"]
    if toast:
        assert toast in labels
    else:
        assert not any(t.startswith(("Not enough", "No swap", "Could not")) for t in labels)
    warnings = [m["message"] for m in r.warnings()]
    assert warnings == ([f"{phone.tool} exited with 1"] if code == 1 else [])


def test_without_pkexec(program, gcc_world, phone):
    (gcc_world.fake_bin.path / "pkexec").unlink()
    r = gcc_world.run(program, [*SHOW, set_active(True, type="AdwSwitchRow", text="Swap File"), wait(300),
                                dump("after")])
    assert r.dump("after").within(**PAGE).one(type="AdwSwitchRow", text="Swap File")["active"] is False
    # Said once: the switch is put back after its own change, so it does not try again
    warnings = [m["message"] for m in r.warnings()]
    assert len(warnings) == 1 and warnings[0].startswith(f"Cannot run pkexec {phone.tool}: ")


def test_without_proc_the_process_rows_stay_empty(program, gcc_world, phone):
    shutil.rmtree(phone.path("/proc"))
    r = gcc_world.run(program, [*SHOW, dump("page")])
    group = r.dump("page").within(type="AdwPreferencesGroup", text="Largest Processes")
    # Rows without a name (not on a phone, where /proc always is there)
    assert group.rows() == [(None, None)] * 6
    assert rows(r.dump("page"))["Move Swap to RAM"] == "Swap is empty"
