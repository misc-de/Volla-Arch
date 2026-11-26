# "Faster GPS Fix" on the Location page (cc-plinius-epo.c): the switch shows enabled= of
# /etc/plinius-epo.conf and runs "pkexec plinius-epo-set on|off" (a fake in the program's root
# that writes the file as the real one does); the row below shows the time of the last download
# from /var/lib/plinius-epo/last, followed with a file monitor.

import pytest

from gcc_harness import (build_panel, call, dump, gcc_session, gcc_world, set_active,  # noqa: F401
                         settle, wait_for)

SET_TOOL = "/usr/lib/plinius-epo/plinius-epo-set"
CONF = "/etc/plinius-epo.conf"
STAMP = "/var/lib/plinius-epo/last"
# 2026-01-02 03:04 UTC (TZ=UTC, C locale: %x is mm/dd/yy)
WHEN, SHOWN = 1767323040, "01/02/26, 03:04"

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-epo.h"

static gboolean set_up;

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *location = adw_preferences_group_new ();
    GtkWidget *apps = adw_preferences_group_new ();

    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (location), "Location Services");
    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (apps), "Apps");
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (location));
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (apps));
    set_up = cc_plinius_epo_setup (t->page);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (!g_str_equal (name, "set_up"))
        return FALSE;
    json_builder_add_boolean_value (result, set_up);
    return TRUE;
}
'''

SUBTITLE = ("Downloads satellite orbit predictions from MediaTek every 6 hours, about 80 KB. The GPS then "
            "finds the position in seconds instead of about a minute. No data is sent.")


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    reps = [(f'"{p}"', f'"{{root}}{p}"') for p in (SET_TOOL, CONF, STAMP, "/var/lib/plinius-epo")]
    return build_panel(gcc_session, tmp_path_factory.mktemp("epo"), "epo", ["cc-plinius-epo.c"], GLUE,
                       replacements={"cc-plinius-epo.c": reps})


class Epo:
    def __init__(self, program, world):
        import shutil
        self.root = program.root
        for d in list(self.root.iterdir()):
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        self.world = world
        self.tool = self.path(SET_TOOL)
        self.code = world.tmp / "code"
        self.tool.parent.mkdir(parents=True)
        self.path(STAMP).parent.mkdir(parents=True)
        self.path(CONF).parent.mkdir(parents=True)
        # As plinius-epo-set: enabled= into the config, the stamp goes with off; the service
        # (here the tool itself) writes the stamp with on, by replacing it
        self.tool.write_text(f"""#!/bin/bash
echo "$1" >> "{world.tmp}/set-args"
code=$(cat "{self.code}" 2>/dev/null || echo 0)
[ "$code" = 0 ] || exit $code
case "$1" in
on) echo enabled=yes > "{self.path(CONF)}"
    echo {WHEN} > "{self.path(STAMP)}.new"; mv "{self.path(STAMP)}.new" "{self.path(STAMP)}" ;;
off) echo enabled=no > "{self.path(CONF)}"; rm -f "{self.path(STAMP)}" ;;
esac
""")
        self.tool.chmod(0o755)
        world.fake_bin.stub("pkexec", 'exec "$@"')

    def path(self, p):
        return self.root / p.lstrip("/")

    def conf(self, text):
        self.path(CONF).write_text(text)

    def stamp(self, text):
        self.path(STAMP).write_text(text)

    def args(self):
        f = self.world.tmp / "set-args"
        return f.read_text().split() if f.exists() else []


@pytest.fixture
def epo(program, gcc_world):
    return Epo(program, gcc_world)


def switch(screen):
    return screen.one(type="AdwSwitchRow", text="Faster GPS Fix")


def last(screen):
    rows = screen.find(type="AdwActionRow", text="Last Download")
    if not rows:
        return None
    return [n["text"] for n in rows[0].walk() if n.get("type") == "GtkLabel"][0]


def test_group_below_the_location_switch(program, gcc_world, epo):
    epo.stamp(f"{WHEN}\n")
    r = gcc_world.run(program, [call("set_up"), dump("main")])
    assert r.value("set_up") is True
    s = r.dump("main")
    assert [g.get("text") for g in s.find(type="AdwPreferencesGroup")] == ["Location Services", None, "Apps"]
    row = switch(s)
    # No config file: on, as plinius-epo reads it
    assert row["subtitle"] == SUBTITLE and row["active"] is True
    assert last(s) == SHOWN
    assert not r.warnings()


def test_nothing_without_the_package(program, gcc_world, epo):
    epo.tool.chmod(0o644)
    r = gcc_world.run(program, [call("set_up"), dump("main")])
    assert r.value("set_up") is False
    assert not r.dump("main").has(type="AdwSwitchRow")


@pytest.mark.parametrize("conf, active", [
    ("enabled=no\n", False),
    ("enabled=yes\n", True),
    ("# a comment\nother=1\n", True),           # no key: on
    ("enabled=no\nenabled=yes\n", True),         # the last line counts
    ("enabled=yes\nenabled= no \n", False),
])
def test_switch_shows_the_config(program, gcc_world, epo, conf, active):
    epo.conf(conf)
    epo.stamp(f"{WHEN}\n")
    r = gcc_world.run(program, [dump("main")])
    assert switch(r.dump("main"))["active"] is active
    # The last download only while on
    assert last(r.dump("main")) == (SHOWN if active else None)
    assert epo.args() == []


@pytest.mark.parametrize("stamp", [None, "", "0\n", "-5\n", "soon\n", "99999999999999\n"])
def test_no_or_bad_stamp_hides_the_last_download(program, gcc_world, epo, stamp):
    if stamp is not None:
        epo.stamp(stamp)
    r = gcc_world.run(program, [dump("main")])
    assert last(r.dump("main")) is None


def test_switch_on_runs_the_tool_and_shows_the_download(program, gcc_world, epo):
    epo.conf("enabled=no\n")
    r = gcc_world.run(program, [set_active(True, type="AdwSwitchRow"),
                                wait_for(type="AdwActionRow", text="Last Download"), dump("on")])
    assert epo.args() == ["on"]
    assert switch(r.dump("on"))["active"] is True
    assert last(r.dump("on")) == SHOWN
    assert not r.warnings()


def test_switch_off_runs_the_tool_and_hides_the_download(program, gcc_world, epo):
    epo.stamp(f"{WHEN}\n")
    r = gcc_world.run(program, [set_active(False, type="AdwSwitchRow"), settle(300), dump("off")])
    assert epo.args() == ["off"]
    assert switch(r.dump("off"))["active"] is False
    assert last(r.dump("off")) is None
    assert epo.path(CONF).read_text() == "enabled=no\n"


def test_tool_failing_puts_the_switch_back(program, gcc_world, epo):
    epo.conf("enabled=no\n")
    epo.code.write_text("127")
    r = gcc_world.run(program, [set_active(True, type="AdwSwitchRow"), settle(300), dump("back")])
    assert epo.args() == ["on"]
    assert switch(r.dump("back"))["active"] is False
    assert [m["message"] for m in r.warnings()] == [
        f"{epo.tool} failed: Child process exited with code 127"]


def test_without_pkexec_the_switch_goes_back(program, gcc_world, epo):
    epo.conf("enabled=no\n")
    (gcc_world.fake_bin.path / "pkexec").unlink()
    r = gcc_world.run(program, [set_active(True, type="AdwSwitchRow"), settle(), dump("back")])
    assert switch(r.dump("back"))["active"] is False
    # Tried once: the switch goes back after GTK is done with the tap, not inside it
    msgs = [m["message"] for m in r.warnings()]
    assert len(msgs) == 1 and msgs[0].startswith(f"Cannot run pkexec {epo.tool}: ")


def test_stamp_written_later_shows_at_once(program, gcc_world, epo):
    stamp = epo.path(STAMP)
    r = gcc_world.run(program, [dump("before"),
                                {"op": "write_file", "path": str(stamp), "text": f"{WHEN}\n"},
                                wait_for(type="AdwActionRow", text="Last Download"), dump("after")])
    assert last(r.dump("before")) is None
    assert last(r.dump("after")) == SHOWN


def test_without_the_stamp_directory(program, gcc_world, epo):
    import shutil
    shutil.rmtree(epo.path(STAMP).parent.parent)
    r = gcc_world.run(program, [call("set_up"), dump("main")])
    assert r.value("set_up") is True
    assert switch(r.dump("main"))["active"] is True
    assert last(r.dump("main")) is None
