# phosh-plinius 0117/0118: a card of a running app swiped at least halfway away and held there for
# a second asks whether to force quit the app. Two parts of the patch are compiled for the host as
# they are: plinius-force-quit.c (c/phosh_force_quit_harness.c), which finds the app's units on
# the user's systemd and kills them, against a fake systemd manager on a private session bus; and
# the hold of swipe-away-bin.c (c/phosh_swipe_hold_harness.c), which decides when it asks.

import os
import re
import signal
import subprocess
import threading
import warnings
from pathlib import Path

import pytest

from conftest import PKG, REPO, compile_c, load_script

PHOSH = PKG / "phosh-plinius"
PATCH = next(PHOSH.glob("0117-overview-*.patch"))
PO_PATCH = next(PHOSH.glob("0118-po-*.patch"))
C = Path(__file__).parent / "c"
DHW = REPO / "tests/daemons-hw/dhw_daemon_bus.py"
LANGS = ["de", "es", "fr", "it", "nl", "pl", "pt", "pt_BR", "ru"]

SYSTEMD_XML = """<node><interface name='org.freedesktop.systemd1.Manager'>
  <method name='ListUnitsByPatterns'>
    <arg type='as' direction='in'/><arg type='as' direction='in'/>
    <arg type='a(ssssssouso)' direction='out'/>
  </method>
  <method name='KillUnit'>
    <arg type='s' direction='in'/><arg type='s' direction='in'/><arg type='i' direction='in'/>
  </method>
</interface></node>"""
OWN_UNIT = "app-gnome-mobi.phosh.Shell-77.scope"


def file_part(patch, path):
    return patch.read_text().split(f"+++ b/{path}\n", 1)[1].split("\ndiff --git", 1)[0]


def new_file(patch, path):
    """A file the patch adds, as it is"""
    lines = file_part(patch, path).splitlines()
    return "\n".join(line[1:] for line in lines if line.startswith("+")) + "\n"


def added(patch, path):
    return [line[1:] for line in file_part(patch, path).splitlines() if line.startswith("+")]


def hold_source():
    code = "\n".join(added(PATCH, "src/swipe-away-bin.c"))
    start = code.index("/* Plinius hold:")
    return code[start:code.index("/* Plinius hold end */")]


def hold_defines():
    return "\n".join(l for l in added(PATCH, "src/swipe-away-bin.c") if l.startswith("#define PLINIUS_HOLD_"))


def hold_value(name):
    for line in hold_defines().splitlines():
        if line.split()[1] == name:
            return float(line.split()[2])
    raise AssertionError(name)


@pytest.fixture(scope="module")
def fq_build(tmp_path_factory):
    d = tmp_path_factory.mktemp("force-quit")
    (d / "phosh-config.h").write_text("")
    for name in ("plinius-force-quit.c", "plinius-force-quit.h"):
        (d / name).write_text(new_file(PATCH, f"src/{name}"))
    (d / "cgroup").write_text(f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{OWN_UNIT}\n")
    helper = d / "stop-app"
    helper.write_text("#!/bin/sh\necho \"$@\" >> \"$0.calls\"\n[ \"$1\" != com.example.broken ]\n")
    helper.chmod(0o755)
    program = compile_c(d / "harness", [C / "phosh_force_quit_harness.c", d / "plinius-force-quit.c"],
                        flags=[f"-I{d}", f'-DPLINIUS_OWN_CGROUP="{d / "cgroup"}"',
                               f'-DPLINIUS_ANDROID_STOP_HELPER="{helper}"'],
                        pkgs=["gio-2.0"])
    return program, helper


@pytest.fixture(scope="module")
def hold_harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("swipe-hold")
    (d / "defines.c").write_text(hold_defines() + "\n")
    (d / "hold.c").write_text(hold_source())
    return compile_c(d / "harness", [C / "phosh_swipe_hold_harness.c"],
                     flags=[f'-DSWIPE_HOLD_DEFINES="{d / "defines.c"}"',
                            f'-DSWIPE_HOLD_SOURCE="{d / "hold.c"}"'],
                     pkgs=["glib-2.0"])


@pytest.fixture
def dhw():
    pytest.importorskip("gi")
    return load_script(DHW, "desk_dhw_daemon_bus_force_quit")


class Systemd:
    """The user's systemd manager as far as force quitting uses it, in a thread of its own"""

    def __init__(self, bus, units):
        from gi.repository import Gio, GLib
        self.GLib = GLib
        self.units = units
        self.calls = []
        self.ctx = GLib.MainContext.new()
        self.loop = GLib.MainLoop.new(self.ctx, False)
        ready = threading.Event()

        def thread():
            self.ctx.push_thread_default()
            self.conn = bus.connect()
            info = Gio.DBusNodeInfo.new_for_xml(SYSTEMD_XML).interfaces[0]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                self.conn.register_object("/org/freedesktop/systemd1", info, self._call, None, None)
            self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "RequestName", GLib.Variant("(su)", ("org.freedesktop.systemd1", 0)),
                                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
            ready.set()
            self.loop.run()
            self.ctx.pop_thread_default()

        self.thread = threading.Thread(target=thread, daemon=True)
        self.thread.start()
        assert ready.wait(10)

    def _call(self, conn, sender, path, iface, method, params, invocation):
        GLib = self.GLib
        args = params.unpack()
        self.calls.append((method, args))
        if method == "ListUnitsByPatterns":
            rows = [(name, "", "loaded", state, "running", "", "/org/freedesktop/systemd1/unit/x", 0, "", "/")
                    for name, state in self.units]
            invocation.return_value(GLib.Variant("(a(ssssssouso))", (rows,)))
        elif args[0].startswith("gone-"):
            invocation.return_dbus_error("org.freedesktop.systemd1.NoSuchUnit", f"Unit {args[0]} not loaded.")
        else:
            invocation.return_value(None)

    def close(self):
        self.ctx.invoke_full(0, lambda: (self.conn.close_sync(None), self.loop.quit(), False)[-1])
        self.thread.join(10)


class World:
    def __init__(self, dhw, fq_build, tmp_path, units=()):
        self.program, self.helper = fq_build
        (tmp_path / "bus").mkdir(parents=True)
        self.tmp = tmp_path
        self.bus = dhw.PrivateBus(tmp_path / "bus")
        self.systemd = Systemd(self.bus, list(units))
        self.env = dhw.sanitized_env(DBUS_SESSION_BUS_ADDRESS=self.bus.address, G_MESSAGES_DEBUG="all")

    def run(self, *args, env=None):
        r = subprocess.run([str(self.program), *args], env=env or self.env, capture_output=True,
                           text=True, timeout=30)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "exit" in r.stdout.splitlines(), r.stdout
        assert "log critical" not in r.stdout, r.stdout
        return r.stdout.splitlines()

    def found(self, *app_ids):
        out = self.run("find", *app_ids)
        return [line.split(" ", 1)[1] for line in out if line.startswith("unit ")], out

    def close(self):
        self.systemd.close()
        self.bus.close()


@pytest.fixture
def world(dhw, fq_build, tmp_path):
    worlds = []

    def make(**kw):
        w = World(dhw, fq_build, tmp_path / str(len(worlds)), **kw)
        worlds.append(w)
        return w

    yield make
    for w in worlds:
        w.close()


# Units and the apps they run

UNITS = {
    # Started by Phosh, GLib or the desktop: the pid at the end
    "app-gnome-org.gnome.Console-1234.scope": "org.gnome.Console",
    "app-flatpak-org.mozilla.firefox-4321.scope": "org.mozilla.firefox",
    "app-desktop-firefox-99.scope": "firefox",
    # An app id with a "-" of its own (escaped by systemd)
    r"app-gnome-org.gnome.Evolution\x2dalarm\x2dnotify-55.scope": "org.gnome.Evolution-alarm-notify",
    # Autostarted, D-Bus activated
    "app-gnome-org.gnome.Software@autostart.service": "org.gnome.Software",
    "app-gnome-org.example.Plain.service": "org.example.Plain",
    "dbus-:1.2-org.gnome.Settings@1.service": "org.gnome.Settings",
    "dbus-:1.13-org.gnome.Calls.service": "org.gnome.Calls",
}
NOT_APPS = [
    "dbus-broker.service",            # the bus itself: no "-" after the bus name
    "dbus.socket",
    "app-gnome-.scope",               # nothing between launcher and the pid
    "app-org.gnome.Foo@autostart.service",  # no launcher: the id is not told apart, so no id
    "app-gnome-org.gnome.Foo.scope",  # a scope without its random part
    "app-gnome-a-b-12.scope",         # a part too many
    "session-2.scope",
    "phosh.service",
    "app.slice",
    "app-gnome-org.gnome.Console-1234.mount",
]


def test_units_tell_their_app(fq_build):
    program, _ = fq_build
    r = subprocess.run([str(program), "unit", *UNITS, *NOT_APPS], capture_output=True, text=True,
                       timeout=30)
    got = dict(line.split(" -> ") for line in r.stdout.splitlines() if " -> " in line)
    for unit, app_id in UNITS.items():
        assert got[unit] == app_id, unit
    for unit in NOT_APPS:
        assert got[unit] == "(none)", unit


def test_android_packages_of_waydroid_windows(fq_build):
    program, _ = fq_build
    good = {"waydroid.com.android.chrome": "com.android.chrome",
            "waydroid.org.example.app_2": "org.example.app_2"}
    bad = ["waydroid.chrome", "waydroid.", "waydroid.com.", "waydroid.com..x", "waydroid.1com.x",
           "waydroid.com.x-y", "waydroid.--help.x", "Waydroid", "org.gnome.Console",
           "waydroid.a." + "b" * 300]
    r = subprocess.run([str(program), "android", *good, *bad], capture_output=True, text=True,
                       timeout=30)
    got = dict(line.split(" -> ") for line in r.stdout.splitlines() if " -> " in line)
    for app_id, package in good.items():
        assert got[app_id] == package
    for app_id in bad:
        assert got[app_id] == "(none)", app_id
    assert got["NULL"] == "(none)"


def test_find_takes_exactly_the_app_s_running_units(world):
    w = world(units=[
        ("app-gnome-org.gnome.Console-1234.scope", "active"),
        ("app-gnome-org.gnome.Console-1300.scope", "deactivating"),
        ("app-gnome-org.gnome.Console-1100.scope", "inactive"),
        ("app-gnome-org.gnome.Console-1000.scope", "failed"),
        # Another app with a longer id, and a terminal's app started from it
        ("app-gnome-org.gnome.Console.Devel-7.scope", "active"),
        ("app-gnome-org.gnome.Nautilus-8.scope", "active"),
        ("dbus-:1.2-org.gnome.Console@1.service", "active"),
    ])
    units, out = w.found("org.gnome.Console")
    assert units == ["app-gnome-org.gnome.Console-1234.scope", "app-gnome-org.gnome.Console-1300.scope",
                     "dbus-:1.2-org.gnome.Console@1.service"]
    assert "found 3" in out
    method, (states, patterns) = w.systemd.calls[0]
    assert method == "ListUnitsByPatterns"
    assert states == [] and patterns == ["app-*.scope", "app-*.service", "dbus-*.service"]
    # Looking kills nothing
    assert [c[0] for c in w.systemd.calls] == ["ListUnitsByPatterns"]


def test_find_by_the_desktop_file_s_id_too(world):
    w = world(units=[("app-gnome-org.mozilla.firefox-5.scope", "active")])
    assert w.found("firefox", "org.mozilla.firefox")[0] == ["app-gnome-org.mozilla.firefox-5.scope"]
    assert w.found("firefox")[0] == []


def test_phosh_s_own_unit_is_never_found(world):
    w = world(units=[(OWN_UNIT, "active"), ("app-gnome-mobi.phosh.Shell-80.scope", "active")])
    assert w.found("mobi.phosh.Shell")[0] == ["app-gnome-mobi.phosh.Shell-80.scope"]


def test_nothing_found(world):
    w = world(units=[("app-gnome-org.gnome.Nautilus-8.scope", "active")])
    units, out = w.found("org.gnome.Console")
    assert units == [] and "found 0" in out


def test_find_without_systemd_is_an_error(world, dhw):
    w = world()
    w.systemd.close()
    out = w.run("find", "org.gnome.Console")
    assert any(line.startswith("error ") for line in out), out
    # And without a session bus at all
    out = w.run("find", "org.gnome.Console", env=dhw.sanitized_env())
    assert any(line.startswith("error ") for line in out), out
    w.systemd = Systemd(w.bus, [])


def test_a_cancelled_search_says_so(world):
    w = world(units=[("app-gnome-org.gnome.Console-1234.scope", "active")])
    assert "cancelled" in w.run("cancel", "org.gnome.Console")


def test_kill_sends_sigkill_to_every_process_of_each_unit(world):
    w = world()
    out = w.run("kill", "app-gnome-org.gnome.Console-1234.scope", "dbus-:1.2-org.gnome.Console@1.service")
    assert [c for c in w.systemd.calls if c[0] == "KillUnit"] == [
        ("KillUnit", ("app-gnome-org.gnome.Console-1234.scope", "all", int(signal.SIGKILL))),
        ("KillUnit", ("dbus-:1.2-org.gnome.Console@1.service", "all", int(signal.SIGKILL))),
    ]
    assert "log message Force quit app-gnome-org.gnome.Console-1234.scope" in out


def test_a_unit_that_went_away_is_a_warning(world):
    w = world()
    out = w.run("kill", "gone-app-gnome-x-1.scope")
    assert any(l.startswith("log warning Failed to force quit gone-app-gnome-x-1.scope") for l in out), out


def test_kill_nothing_or_without_a_bus(world, dhw):
    w = world()
    w.run("kill")
    assert w.systemd.calls == []
    out = w.run("kill", "app-gnome-x-1.scope", env=dhw.sanitized_env())
    assert any("no session bus" in l for l in out), out


def fake_pkexec(tmp):
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    pkexec = bin_dir / "pkexec"
    pkexec.write_text("#!/bin/sh\nexec \"$@\"\n")
    pkexec.chmod(0o755)
    return bin_dir


def test_android_app_is_stopped_through_pkexec(world, fq_build, tmp_path):
    w = world()
    _, helper = fq_build
    calls = Path(f"{helper}.calls")
    calls.unlink(missing_ok=True)
    env = dict(w.env, PATH=f"{fake_pkexec(tmp_path)}:/usr/bin:/bin")
    out = w.run("stop", "com.android.chrome", env=env)
    assert "started" in out and "log message Force quit com.android.chrome" in out
    out = w.run("stop", "com.example.broken", env=env)
    assert any(l.startswith("log warning Failed to force quit com.example.broken") for l in out), out
    assert calls.read_text().split() == ["com.android.chrome", "com.example.broken"]


def test_android_stop_without_the_helper_or_pkexec(world, fq_build, tmp_path):
    w = world()
    _, helper = fq_build
    out = w.run("stop", "com.android.chrome", env=dict(w.env, PATH=str(tmp_path / "empty")))
    assert any(l.startswith("error ") for l in out), out
    helper.chmod(0o644)
    try:
        out = w.run("stop", "com.android.chrome")
    finally:
        helper.chmod(0o755)
    assert any(l.startswith("error ") and "is not installed" in l for l in out), out


# The hold that asks

class Hold:
    def __init__(self, program):
        self.proc = subprocess.Popen([str(program)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True)
        assert self.proc.stdout.readline().strip() == "ready"

    def do(self, *commands):
        """Run commands; returns what they printed besides "done" """
        out = []
        for command in commands:
            self.proc.stdin.write(command + "\n")
            self.proc.stdin.flush()
            while (line := self.proc.stdout.readline().strip()) != "done":
                assert line, "the harness ended"
                out.append(line)
        return out

    def close(self):
        self.proc.stdin.write("quit\n")
        self.proc.stdin.close()
        assert self.proc.stdout.read().split() == ["exit"]
        assert self.proc.wait(timeout=10) == 0


@pytest.fixture
def hold(hold_harness):
    h = Hold(hold_harness)
    yield h
    h.close()


MS = int(hold_value("PLINIUS_HOLD_MS"))


def test_hold_values():
    assert hold_value("PLINIUS_HOLD_PROGRESS") == 0.5
    assert MS == 1000
    assert 0 < hold_value("PLINIUS_HOLD_SLACK") < 0.1


def test_held_halfway_for_a_second_asks_and_the_card_comes_back(hold):
    assert hold.do("ask 1", "begin", "progress 0.3", "progress 0.55") == []
    assert hold.do(f"wait {MS - 300}") == []
    assert hold.do("wait 400") == ["held"]
    # Asked once, however long it is held; released, the card goes back instead of closing
    assert hold.do("progress 0.6", f"wait {MS + 200}") == []
    assert hold.do("end 1") == ["animate 0"]
    # The next swipe closes as before
    assert hold.do("begin", "progress 0.7", "end 1") == ["animate 1"]


def test_downwards_and_negative_progress_count_too(hold):
    assert hold.do("ask 1", "begin", "progress -0.6", f"wait {MS + 200}") == ["held"]
    assert hold.do("end -1") == ["animate 0"]


def test_moving_starts_the_second_again(hold):
    hold.do("ask 1", "begin", "progress 0.5")
    hold.do(f"wait {MS - 400}")
    # Small shaking keeps the time, a real move starts it again
    hold.do("progress 0.53", "progress 0.52")
    assert hold.do("progress 0.7", f"wait {MS - 300}") == []
    assert hold.do("wait 500") == ["held"]


def test_slight_shaking_keeps_the_time(hold):
    hold.do("ask 1", "begin", "progress 0.6", f"wait {MS // 2}")
    assert hold.do("progress 0.65", "progress 0.58", f"wait {MS // 2 + 200}") == ["held"]


def test_back_below_half_forgets_the_hold(hold):
    hold.do("ask 1", "begin", "progress 0.6", f"wait {MS // 2}")
    assert hold.do("progress 0.4", f"wait {MS + 200}") == []
    # A quick swipe still closes, a swipe given up still goes back
    assert hold.do("progress 0.8", "end 1") == ["animate 1"]
    assert hold.do("begin", "progress 0.2", "end 0") == ["animate 0"]


def test_released_before_the_second_closes(hold):
    hold.do("ask 1", "begin", "progress 0.6", f"wait {MS // 2}")
    assert hold.do("end 1", f"wait {MS}") == ["animate 1"]


def test_a_new_swipe_forgets_the_old_hold(hold):
    hold.do("ask 1", "begin", "progress 0.6", f"wait {MS // 2}")
    assert hold.do("begin", f"wait {MS}") == []


def test_without_hold_to_ask_nothing_changes(hold):
    # Notifications and dialogs: no question, the swipe ends where it was going
    assert hold.do("ask 0", "begin", "progress 0.6", f"wait {MS + 200}", "end 1") == ["animate 1"]
    # Switched off while waiting: nothing comes
    hold.do("ask 1", "begin", "progress 0.6")
    assert hold.do("ask 0", f"wait {MS + 200}", "end 1") == ["animate 1"]


# The patch around it

def overview_strings():
    code = "\n".join(added(PATCH, "src/overview.c"))
    return re.findall(r'_\("((?:[^"\\]|\\.)*)"\)', code)


def po_entries(lang):
    part = file_part(PO_PATCH, f"po/{lang}.po")
    lines = [l[1:] for l in part.splitlines() if l.startswith("+")]
    entries = {}
    for i, line in enumerate(lines):
        if line.startswith("msgid "):
            fmt = i > 0 and lines[i - 1] == "#, c-format"
            entries[line[7:-1]] = (lines[i + 1][8:-1], fmt)
    return entries


def test_every_string_of_the_dialog_is_translated():
    strings = overview_strings()
    assert set(strings) == {"Force Quit %s?", "The app ends at once. Anything not saved is lost.",
                            "Cannot Force Quit %s", "No running process of the app was found.",
                            "Cancel", "Close", "Force Quit"}
    for lang in LANGS:
        entries = po_entries(lang)
        for s in strings:
            if s == "Cancel":
                continue  # Phosh's own
            assert s in entries, (lang, s)
            msgstr, fmt = entries[s]
            assert msgstr and msgstr != s, (lang, s)
            # Each %s comes back exactly once, marked as a format
            assert msgstr.count("%s") == s.count("%s"), (lang, s)
            assert fmt == ("%s" in s), (lang, s)


def test_the_po_files_compile(tmp_path):
    import shutil
    if shutil.which("msgfmt") is None:
        pytest.skip("no msgfmt")
    for lang in LANGS:
        po = tmp_path / f"{lang}.po"
        entries = po_entries(lang)
        body = 'msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n'
        for msgid, (msgstr, fmt) in entries.items():
            body += ("\n#, c-format" if fmt else "\n") + f'\nmsgid "{msgid}"\nmsgstr "{msgstr}"\n'
        po.write_text(body)
        r = subprocess.run(["msgfmt", "-c", "-o", os.devnull, str(po)], capture_output=True, text=True)
        assert r.returncode == 0, (lang, r.stderr)


def test_pkgbuild_applies_both_patches_last_and_builds_the_new_file():
    pkgbuild = (PHOSH / "PKGBUILD").read_text()
    applied = re.findall(r'patch -d phosh -Np1 -i "\$srcdir/(\d{4})-', pkgbuild)
    assert applied[-2:] == ["0117", "0118"]
    assert PATCH.name in pkgbuild and PO_PATCH.name in pkgbuild
    meson = added(PATCH, "src/meson.build")
    assert meson == ["  'plinius-force-quit.c',"]


def test_only_the_cards_ask():
    # The bins of notifications and dialogs keep removing: only the activity switches it on
    activity = "\n".join(added(PATCH, "src/activity.c"))
    assert "phosh_swipe_away_bin_set_hold_to_ask (PHOSH_SWIPE_AWAY_BIN (priv->swipe_bin), TRUE);" in activity
    bin_code = "\n".join(added(PATCH, "src/swipe-away-bin.c"))
    assert "self->hold_to_ask = TRUE" not in bin_code
    overview = "\n".join(added(PATCH, "src/overview.c"))
    # Asked only for a card with a window, gone with the window and when the phone locks
    assert '"swapped-signal::plinius-force-quit", on_plinius_force_quit' in overview
    assert "if (priv->plinius_fq_activity == activity)\n    plinius_fq_close (overview);" in overview
    assert '"notify::locked"' in overview
