# "Automatic Connections" on the Privacy page (cc-plinius-connections.c):
# - "Search for Updates Automatically" shows whether the user's unit directory masks
#   plinius-software-check.timer and masks it ("systemctl --user mask --now") or unmasks and starts
#   it; systemctl is a fake that makes and removes the mask link as systemd does.
# - "Connection Check" and its server come from NetworkManager's 90-plinius-connectivity.conf in
#   the program's root, written by "pkexec plinius-connectivity-set off | on URI RESPONSE"; the
#   tool is a fake in the root that writes the file as the real one does. Without the file, Arch's
#   default applies (on, ping.archlinux.org).

import shutil

import pytest

from gcc_harness import (apply, build_panel, call, dump, gcc_session, gcc_world, select,  # noqa: F401
                         set_active, set_text, settle)

TIMER = "plinius-software-check.timer"
UNIT_DIR = "/usr/lib/systemd/user/"
TOOL = "/usr/lib/plinius/plinius-connectivity-set"
CONF = "/etc/NetworkManager/conf.d/90-plinius-connectivity.conf"

ARCH = ("http://ping.archlinux.org/nm-check.txt", "NetworkManager is online")
GNOME = ("http://nmcheck.gnome.org/check_network_status.txt", "NetworkManager is online")
UBUNTU = ("http://connectivity-check.ubuntu.com./", "")
ITEMS = ["Arch Linux (ping.archlinux.org)", "GNOME (nmcheck.gnome.org)",
         "Ubuntu (connectivity-check.ubuntu.com)", "Own Server"]
OWN_HINT = ("The server must answer the address with the expected text, or with nothing when it is "
            "left empty.")
REFUSED = "The address or the answer was not accepted."

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-connections.h"

static gboolean added;

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *group = adw_preferences_group_new ();

    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (group), "Devices");
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (group));
    added = cc_plinius_connections_add (t->page);
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
    reps = [(f'"{p}"', f'"{{root}}{p}"') for p in (UNIT_DIR, TOOL, CONF)]
    return build_panel(gcc_session, tmp_path_factory.mktemp("connections"), "connections",
                       ["cc-plinius-connections.c"], GLUE,
                       replacements={"cc-plinius-connections.c": reps},
                       allow=("/dev/null",))  # only compared with the mask link's target


class Phone:
    """The timer, the tool and the config in the program's root; systemctl and pkexec as fakes."""

    def __init__(self, program, world):
        self.root = program.root
        for d in list(self.root.iterdir()):
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        self.world = world
        self.mask = world.home / ".config/systemd/user" / TIMER
        self.code = world.tmp / "code"
        self.systemctl_code = world.tmp / "systemctl-code"
        self.path(CONF).parent.mkdir(parents=True)
        self.timer(True)
        self.tool(True)
        # As systemd: mask links the unit to /dev/null in the user's directory, unmask removes it
        world.fake_bin.stub("systemctl", f"""
code=$(cat "{self.systemctl_code}" 2>/dev/null || echo 0)
[ "$code" = 0 ] || exit $code
case "$2" in
mask) mkdir -p "{self.mask.parent}"; ln -sf /dev/null "{self.mask}" ;;
unmask) rm -f "{self.mask}" ;;
esac""")
        world.fake_bin.stub("pkexec", 'exec "$@"')

    def path(self, p):
        return self.root / p.lstrip("/")

    def timer(self, present):
        unit = self.path(UNIT_DIR + TIMER)
        if present:
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text("[Timer]\nOnCalendar=00/4:00\n")
        elif unit.exists():
            unit.unlink()

    def tool(self, present):
        tool = self.path(TOOL)
        if not present:
            tool.unlink(missing_ok=True)
            return
        tool.parent.mkdir(parents=True, exist_ok=True)
        # As plinius-connectivity-set: the whole file anew
        tool.write_text(f"""#!/bin/bash
printf '%s\\0' "$@" >> "{self.world.tmp}/tool-args"; printf '\\036' >> "{self.world.tmp}/tool-args"
code=$(cat "{self.code}" 2>/dev/null || echo 0)
[ "$code" = 0 ] || exit $code
case "$1" in
off) printf '[connectivity]\\nenabled=false\\n' > "{self.path(CONF)}" ;;
on) printf '[connectivity]\\nenabled=true\\nuri=%s\\nresponse=%s\\n' "$2" "$3" > "{self.path(CONF)}" ;;
esac
""")
        tool.chmod(0o755)

    def conf(self, text):
        self.path(CONF).write_text(text)

    def masked(self, yes=True):
        if yes:
            self.mask.parent.mkdir(parents=True, exist_ok=True)
            self.mask.symlink_to("/dev/null")
        else:
            self.mask.unlink(missing_ok=True)

    def tool_calls(self):
        f = self.world.tmp / "tool-args"
        if not f.exists():
            return []
        return [r.split("\0")[:-1] for r in f.read_text().split("\036")[:-1]]

    def systemctl_calls(self):
        return self.world.fake_bin.calls("systemctl")


@pytest.fixture
def phone(program, gcc_world):
    return Phone(program, gcc_world)


def updates(screen):
    rows = screen.find(type="AdwSwitchRow", text="Search for Updates Automatically")
    return rows[0] if rows else None


def check(screen):
    rows = screen.find(type="AdwSwitchRow", text="Connection Check")
    return rows[0] if rows else None


def server(screen):
    rows = screen.find(type="AdwComboRow", text="Check Server")
    return rows[0] if rows else None


def entries(screen):
    return {n["text"]: n for n in screen.find(type="AdwEntryRow")}


def test_group_at_the_end_with_arch_as_the_default(program, gcc_world, phone):
    r = gcc_world.run(program, [call("added"), dump("main")])
    assert r.value("added") is True
    s = r.dump("main")
    assert [g.get("text") for g in s.find(type="AdwPreferencesGroup")] == ["Devices", "Automatic Connections"]
    row = updates(s)
    assert row["active"] is True
    assert row["subtitle"] == ("Every 4 hours the phone asks plinius.cais.de and the servers of Arch Linux ARM "
                               "for new versions. When off, look for them yourself under System › Updates.")
    row = check(s)
    assert row["active"] is True
    assert row["subtitle"].startswith("NetworkManager regularly asks a server whether the internet is reachable")
    # No config of ours: Arch's own, the first server; the own server's rows are hidden
    combo = server(s)
    assert combo["items"] == ITEMS and combo["selected"] == 0 and "subtitle" not in combo
    assert entries(s) == {}
    assert phone.tool_calls() == [] and phone.systemctl_calls() == []
    assert not r.warnings()


def test_nothing_without_timer_and_tool(program, gcc_world, phone):
    phone.timer(False)
    phone.tool(False)
    r = gcc_world.run(program, [call("added"), dump("main")])
    assert r.value("added") is False
    assert [g.get("text") for g in r.dump("main").find(type="AdwPreferencesGroup")] == ["Devices"]


def test_only_what_is_installed_shows(program, gcc_world, phone):
    phone.tool(False)
    r = gcc_world.run(program, [dump("timer only")])
    s = r.dump("timer only")
    assert updates(s) and not check(s) and not server(s) and entries(s) == {}

    phone.tool(True)
    phone.timer(False)
    phone.conf(f"[connectivity]\nuri=http://192.0.2.7/ok\nresponse=fine\n")
    r = gcc_world.run(program, [dump("tool only")])
    s = r.dump("tool only")
    assert not updates(s) and check(s) and server(s)["selected"] == 3 and set(entries(s)) == {
        "Server Address", "Expected Answer"}


def test_masked_timer_shows_off(program, gcc_world, phone):
    phone.masked()
    r = gcc_world.run(program, [dump("main")])
    assert updates(r.dump("main"))["active"] is False


def test_switching_updates_off_masks_and_stops_the_timer(program, gcc_world, phone):
    r = gcc_world.run(program, [set_active(False, type="AdwSwitchRow", text="Search for Updates Automatically"),
                                settle(300), dump("off")])
    assert phone.systemctl_calls() == [["--user", "mask", "--now", TIMER]]
    assert phone.mask.is_symlink()
    assert updates(r.dump("off"))["active"] is False and "sensitive" not in updates(r.dump("off"))
    assert not r.warnings()


def test_switching_updates_on_unmasks_and_starts_the_timer(program, gcc_world, phone):
    phone.masked()
    r = gcc_world.run(program, [set_active(True, type="AdwSwitchRow", text="Search for Updates Automatically"),
                                settle(300), dump("on")])
    assert phone.systemctl_calls() == [["--user", "unmask", TIMER], ["--user", "start", TIMER]]
    assert not phone.mask.exists()
    assert updates(r.dump("on"))["active"] is True
    assert not r.warnings()


def test_systemctl_failing_puts_the_switch_back(program, gcc_world, phone):
    phone.masked()
    phone.systemctl_code.write_text("1")
    r = gcc_world.run(program, [set_active(True, type="AdwSwitchRow", text="Search for Updates Automatically"),
                                settle(300), dump("back")])
    assert phone.systemctl_calls() == [["--user", "unmask", TIMER]]
    assert updates(r.dump("back"))["active"] is False
    assert [m["message"] for m in r.warnings()] == [
        f"systemctl --user unmask {TIMER} failed: Child process exited with code 1"]


def test_without_systemctl_the_switch_goes_back(program, gcc_world, phone):
    (gcc_world.fake_bin.path / "systemctl").unlink()
    r = gcc_world.run(program, [set_active(False, type="AdwSwitchRow", text="Search for Updates Automatically"),
                                settle(), dump("back")])
    assert updates(r.dump("back"))["active"] is True
    assert [m["message"] for m in r.warnings()] == ["Cannot run systemctl --user: not found"]


@pytest.mark.parametrize("conf, active, selected, own", [
    ("[connectivity]\nenabled=false\n", False, 0, None),
    ("[connectivity]\nenabled=true\n", True, 0, None),
    ("[connectivity]\nenabled=maybe\n", True, 0, None),                 # unreadable value: as NM, on
    (f"[connectivity]\nuri={GNOME[0]}\n", True, 1, None),               # no response: NM's default
    (f"[connectivity]\nuri={GNOME[0]}\nresponse={GNOME[1]}\n", True, 1, None),
    (f"[connectivity]\nuri={UBUNTU[0]}\nresponse=\n", True, 2, None),
    (f"[connectivity]\nuri={UBUNTU[0]}\n", True, 3, (UBUNTU[0], "NetworkManager is online")),
    ("[connectivity]\nuri=  \n", True, 0, None),                        # an empty address: Arch's
    ("[connectivity]\nuri=http://192.0.2.7/ok\nresponse= fine \n", True, 3, ("http://192.0.2.7/ok", "fine")),
    ("not a key file\n", True, 0, None),
])
def test_rows_show_the_config(program, gcc_world, phone, conf, active, selected, own):
    phone.conf(conf)
    r = gcc_world.run(program, [dump("main")])
    s = r.dump("main")
    assert check(s)["active"] is active
    if not active:
        assert server(s) is None and entries(s) == {}
        return
    assert server(s)["selected"] == selected
    if own:
        e = entries(s)
        assert (e["Server Address"]["value"], e["Expected Answer"]["value"]) == own
        assert server(s)["subtitle"] == OWN_HINT
    else:
        assert entries(s) == {}
    assert phone.tool_calls() == []


def test_switching_the_check_off_and_on(program, gcc_world, phone):
    r = gcc_world.run(program, [set_active(False, type="AdwSwitchRow", text="Connection Check"),
                                settle(300), dump("off"),
                                set_active(True, type="AdwSwitchRow", text="Connection Check"),
                                settle(300), dump("on")])
    assert phone.tool_calls() == [["off"], ["on", *ARCH]]
    assert check(r.dump("off"))["active"] is False and server(r.dump("off")) is None
    assert server(r.dump("on"))["selected"] == 0
    assert phone.path(CONF).read_text() == f"[connectivity]\nenabled=true\nuri={ARCH[0]}\nresponse={ARCH[1]}\n"
    assert not r.warnings()


@pytest.mark.parametrize("index, expect", [(1, GNOME), (2, UBUNTU)])
def test_picking_a_known_server_writes_it(program, gcc_world, phone, index, expect):
    r = gcc_world.run(program, [select(index, type="AdwComboRow", text="Check Server"), settle(300), dump("picked")])
    assert phone.tool_calls() == [["on", *expect]]
    assert server(r.dump("picked"))["selected"] == index
    assert not r.warnings()


def test_own_server_is_written_once_its_address_is_applied(program, gcc_world, phone):
    r = gcc_world.run(program, [select(3, type="AdwComboRow", text="Check Server"), settle(), dump("own"),
                                set_text("http://192.0.2.7/check", type="AdwEntryRow", text="Server Address"),
                                set_text("ok", type="AdwEntryRow", text="Expected Answer"),
                                apply(type="AdwEntryRow", text="Server Address"), settle(300), dump("written")])
    # Picking it alone writes nothing: the rows start with the address in use
    e = entries(r.dump("own"))
    assert (e["Server Address"]["value"], e["Expected Answer"]["value"]) == ARCH
    assert server(r.dump("own"))["subtitle"] == OWN_HINT
    assert phone.tool_calls() == [["on", "http://192.0.2.7/check", "ok"]]
    s = r.dump("written")
    assert server(s)["selected"] == 3
    assert entries(s)["Server Address"]["value"] == "http://192.0.2.7/check"
    assert not r.warnings()


def test_refused_own_server_keeps_the_typed_address(program, gcc_world, phone):
    phone.code.write_text("3")
    r = gcc_world.run(program, [select(3, type="AdwComboRow", text="Check Server"), settle(),
                                set_text("ftp://192.0.2.7", type="AdwEntryRow", text="Server Address"),
                                apply(type="AdwEntryRow", text="Server Address"), settle(300), dump("refused")])
    s = r.dump("refused")
    e = entries(s)
    assert e["Server Address"]["value"] == "ftp://192.0.2.7" and "error" in e["Server Address"].get("classes", [])
    assert server(s)["selected"] == 3 and server(s)["subtitle"] == REFUSED
    assert [m["message"] for m in r.warnings()] == [f"{phone.path(TOOL)} failed: Child process exited with code 3"]

    # Applying again clears the mark and tries anew
    phone.code.write_text("0")
    r = gcc_world.run(program, [select(3, type="AdwComboRow", text="Check Server"), settle(),
                                set_text("http://192.0.2.7/", type="AdwEntryRow", text="Server Address"),
                                apply(type="AdwEntryRow", text="Expected Answer"), settle(300), dump("again")])
    e = entries(r.dump("again"))
    assert "error" not in e["Server Address"].get("classes", [])
    assert server(r.dump("again"))["subtitle"] == OWN_HINT


def test_tool_failing_for_a_known_server_puts_the_rows_back(program, gcc_world, phone):
    phone.code.write_text("1")
    r = gcc_world.run(program, [select(1, type="AdwComboRow", text="Check Server"), settle(300), dump("back")])
    assert phone.tool_calls() == [["on", *GNOME]]
    assert server(r.dump("back"))["selected"] == 0
    assert [m["message"] for m in r.warnings()] == [f"{phone.path(TOOL)} failed: Child process exited with code 1"]


def test_without_pkexec_the_check_goes_back(program, gcc_world, phone):
    (gcc_world.fake_bin.path / "pkexec").unlink()
    r = gcc_world.run(program, [set_active(False, type="AdwSwitchRow", text="Connection Check"), settle(),
                                dump("back")])
    assert check(r.dump("back"))["active"] is True
    msgs = [m["message"] for m in r.warnings()]
    assert len(msgs) == 1 and msgs[0].startswith(f"Cannot run pkexec {phone.path(TOOL)}: ")
