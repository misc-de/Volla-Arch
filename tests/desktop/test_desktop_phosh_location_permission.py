# phosh-plinius 0112: native apps (geoclue's AuthorizeApp) ask for the location with Allow Once,
# Always Allow and Don't Allow, and the answer is remembered in the permission store as
# xdg-desktop-portal remembers it for Flatpak apps: table and id "location", the app's entry
# [accuracy, last use]. location-permission.c of the patch looks the entry up, decides whether
# to ask, and writes the answer; it is taken from the patch as it is and compiled into
# c/phosh_location_permission_harness.c, run once per call against a fake permission store on a
# private session bus.

import subprocess
import threading
import warnings
from pathlib import Path

import pytest

from conftest import PKG, REPO, compile_c, load_script

PHOSH = PKG / "phosh-plinius"
PATCH = next(PHOSH.glob("0112-location-manager-*.patch"))
ONCE_PATCH = next(PHOSH.glob("0109-portal-access-manager-*.patch"))
HARNESS = Path(__file__).parent / "c/phosh_location_permission_harness.c"
DHW = REPO / "tests/daemons-hw/dhw_daemon_bus.py"

STORE_NAME = "org.freedesktop.impl.portal.PermissionStore"
STORE_PATH = "/org/freedesktop/impl/portal/PermissionStore"
STORE_XML = f"""<node><interface name='{STORE_NAME}'>
  <method name='Lookup'><arg direction='in' type='s'/><arg direction='in' type='s'/>
    <arg direction='out' type='a{{sas}}'/><arg direction='out' type='v'/></method>
  <method name='SetPermission'><arg direction='in' type='s'/><arg direction='in' type='b'/>
    <arg direction='in' type='s'/><arg direction='in' type='s'/><arg direction='in' type='as'/></method>
</interface></node>"""
APP = "org.gnome.Maps"
# geoclue's accuracy levels
COUNTRY, CITY, NEIGHBORHOOD, STREET, EXACT = 1, 4, 5, 6, 8


def new_file(patch, name):
    """A file the patch adds, as it is after the patch"""
    part = patch.read_text().split(f"+++ b/{name}\n", 1)[1].split("\ndiff --git", 1)[0]
    lines = part.splitlines()[1:]  # the hunk header
    assert all(line.startswith("+") for line in lines), name
    return "\n".join(line[1:] for line in lines) + "\n"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("location-permission")
    for name in ("location-permission.c", "location-permission.h"):
        (d / name).write_text(new_file(PATCH, f"src/{name}"))
    (d / "access-once.h").write_text(new_file(ONCE_PATCH, "src/access-once.h"))
    return compile_c(d / "harness", [HARNESS],
                     flags=[f"-I{d}", f'-DLOCATION_PERMISSION_SOURCE="{d / "location-permission.c"}"'],
                     pkgs=["gio-unix-2.0"])


@pytest.fixture
def dhw():
    pytest.importorskip("gi")
    return load_script(DHW, "desk_dhw_daemon_bus")


class Store:
    """xdg-permission-store as far as location-permission.c uses it, in a thread of its own.
    `entries`: {app: [permissions]} of the table "location", id "location"; None: no entry for
    the id (NotFound). `fail`: SetPermission refused."""

    def __init__(self, bus, entries=None, fail=False):
        from gi.repository import Gio, GLib
        self.GLib = GLib
        self.entries = entries
        self.fail = fail
        self.calls = []
        self.ctx = GLib.MainContext.new()
        self.loop = GLib.MainLoop.new(self.ctx, False)
        ready = threading.Event()

        def thread():
            self.ctx.push_thread_default()
            self.conn = bus.connect()
            info = Gio.DBusNodeInfo.new_for_xml(STORE_XML).interfaces[0]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                self.conn.register_object(STORE_PATH, info, self._call, None, None)
            self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "RequestName", GLib.Variant("(su)", (STORE_NAME, 0)), GLib.VariantType("(u)"),
                                Gio.DBusCallFlags.NONE, 5000, None)
            ready.set()
            self.loop.run()
            self.ctx.pop_thread_default()

        self.thread = threading.Thread(target=thread, daemon=True)
        self.thread.start()
        assert ready.wait(10)

    def _call(self, conn, sender, path, iface, method, params, invocation):
        args = params.unpack()
        self.calls.append((method, args))
        GLib = self.GLib
        if method == "Lookup":
            if args == ("location", "location") and self.entries is not None:
                invocation.return_value(GLib.Variant("(a{sas}v)", (self.entries, GLib.Variant("y", 0))))
            else:
                invocation.return_dbus_error("org.freedesktop.portal.Error.NotFound", f"No entry for {args[1]}")
        elif self.fail:
            invocation.return_dbus_error("org.freedesktop.portal.Error.NotAllowed", "Not allowed")
        else:
            invocation.return_value(None)

    def written(self):
        return [c[1] for c in self.calls if c[0] == "SetPermission"]

    def close(self):
        self.ctx.invoke_full(0, lambda: (self.conn.close_sync(None), self.loop.quit(), False)[-1])
        self.thread.join(10)


class World:
    """A private session bus, with a fake store unless store=False"""

    def __init__(self, dhw, harness, tmp_path, store=True, **kw):
        self.harness = harness
        tmp_path.mkdir(parents=True)
        self.bus = dhw.PrivateBus(tmp_path)
        self.store = Store(self.bus, **kw) if store else None
        self.env = dhw.sanitized_env(DBUS_SESSION_BUS_ADDRESS=self.bus.address, G_MESSAGES_DEBUG="all",
                                     G_DEBUG="fatal-criticals")

    def run(self, *args):
        r = subprocess.run([str(self.harness), *map(str, args)], env=self.env, capture_output=True,
                           text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        self.out, self.err = r.stdout, r.stderr
        # GLib writes debug messages to stdout too: the harness's answer is the first other line
        return next(line for line in r.stdout.splitlines() if "-DEBUG:" not in line)

    def close(self):
        if self.store:
            self.store.close()
        self.bus.close()


@pytest.fixture
def world(dhw, harness, tmp_path):
    worlds = []

    def make(**kw):
        w = World(dhw, harness, tmp_path / str(len(worlds)), **kw)
        worlds.append(w)
        return w

    yield make
    for w in worlds:
        w.close()


def run_without_bus(harness, dhw, *args):
    env = dhw.sanitized_env(G_MESSAGES_DEBUG="all", G_DEBUG="fatal-criticals")
    r = subprocess.run([str(harness), *map(str, args)], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r


@pytest.mark.parametrize("level,name", [
    (0, "NONE"), (1, "COUNTRY"), (2, "COUNTRY"), (3, "COUNTRY"), (4, "CITY"), (5, "NEIGHBORHOOD"),
    (6, "STREET"), (7, "STREET"), (8, "EXACT"), (9, "EXACT"),
])
def test_level_names_are_xdg_desktop_portal_s(world, level, name):
    w = world(store=False)
    assert w.run("name", level) == name
    if level in (0, 1, 4, 5, 6, 8):
        assert w.run("level", name) == str(level)


def test_an_unknown_name_is_none_as_in_xdg_desktop_portal(world):
    w = world(store=False)
    assert w.run("level", "everywhere") == "0"
    assert "Unknown accuracy 'everywhere', taken as NONE" in w.out


def test_without_entries_it_asks(world):
    assert world(store=False).run("decide", APP, EXACT) == "ask"


@pytest.mark.parametrize("entry,level,decision", [
    (["NONE", "17"], COUNTRY, "deny"),             # Don't Allow, or switched off in GNOME Settings
    (["EXACT", "17"], EXACT, "allow"),             # switched on in GNOME Settings
    (["EXACT", "17"], CITY, "allow"),              # a coarser level than remembered
    (["CITY", "17"], CITY, "allow"),
    (["CITY", "17"], STREET, "ask"),               # a finer one is asked anew
    (["STREET", "17", "x"], NEIGHBORHOOD, "allow"),
    (["somewhere", "17"], COUNTRY, "deny"),        # unknown: NONE, as in xdg-desktop-portal
    (["EXACT"], COUNTRY, "ask"),                   # malformed: ignored, as in xdg-desktop-portal
    ([], COUNTRY, "ask"),
])
def test_the_app_s_entry_decides(world, entry, level, decision):
    w = world(entries={APP: entry, "org.example.Other": ["NONE", "1"]})
    assert w.run("lookup", APP, level) == f"{decision} store"
    assert w.store.calls == [("Lookup", ("location", "location"))]


def test_an_app_without_entry_is_asked(world):
    w = world(entries={"org.example.Other": ["EXACT", "1"]})
    assert w.run("lookup", APP, EXACT) == "ask store"


def test_no_entry_for_the_id_yet_is_asked_and_stored(world):
    w = world(entries=None)
    assert w.run("lookup", APP, EXACT) == "ask store"


def test_without_a_permission_store_it_asks_and_stores_nothing(world):
    w = world(store=False)
    assert w.run("lookup", APP, EXACT) == "ask nostore"
    assert "No permission store, asking without remembering" in w.out


def test_without_a_session_bus_it_asks_and_stores_nothing(harness, dhw):
    r = run_without_bus(harness, dhw, "lookup", APP, EXACT)
    assert r.stdout.splitlines()[-1] == "ask nostore"
    assert "No session bus, asking without remembering" in r.stdout


def test_a_cancelled_lookup_is_no_answer(world):
    w = world(entries={APP: ["EXACT", "1"]})
    assert w.run("lookup", APP, EXACT, "cancel") == "cancelled nostore"


@pytest.mark.parametrize("level,choice,accuracy", [
    (EXACT, "always", "EXACT"),
    (CITY, "always", "CITY"),
    (7, "always", "STREET"),       # a level between geoclue's: the next coarser name
    (0, "always", "COUNTRY"),      # never NONE for an allowed app
    (EXACT, "never", "NONE"),
    (CITY, "never", "NONE"),
])
def test_always_and_never_are_remembered(world, level, choice, accuracy):
    w = world()
    assert w.run("store", APP, level, choice) == "written"
    [(table, create, id_, app, perms)] = w.store.written()
    assert (table, create, id_, app) == ("location", True, "location", APP)
    assert perms[0] == accuracy and len(perms) == 2
    # The last use as xdg-desktop-portal writes it: microseconds of g_get_monotonic_time()
    assert perms[1].isdigit() and int(perms[1]) > 0
    assert f"Remembered {accuracy} for {APP}" in w.out


@pytest.mark.parametrize("choice", ["once", "cancel"])
def test_once_and_a_cancelled_dialog_store_nothing(world, choice):
    w = world()
    assert w.run("store", APP, EXACT, choice) == "not written"
    assert w.store.written() == []


def test_a_write_the_store_refuses_is_reported(world):
    w = world(fail=True)
    assert w.run("store", APP, EXACT, "always") == "written"
    assert f"Could not remember EXACT for {APP}: " in w.err


def test_without_a_session_bus_nothing_is_written(harness, dhw):
    r = run_without_bus(harness, dhw, "store", APP, EXACT, "never")
    assert r.stdout.splitlines()[-1] == "not written"
    assert f"Could not remember NONE for {APP}: " in r.stderr
