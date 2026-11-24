# phosh-plinius 0109: every access dialog asks with "Allow Once", "Always Allow" and "Don't Allow".
# access-once.c of the patch answers the dialog and, for "Allow Once" and a dialog swiped away,
# deletes the entry xdg-desktop-portal then writes into its permission store. The file is taken
# from the patch as it is and compiled into c/phosh_access_once_harness.c, which serves the
# answers on a private session bus; a fake permission store on that bus answers Lookup and
# DeletePermission and emits Changed as xdg-permission-store does.

import threading
import time
import warnings
from pathlib import Path

import pytest

from conftest import PKG, REPO, compile_c, load_script

PHOSH = PKG / "phosh-plinius"
PATCH = next(PHOSH.glob("0109-portal-access-manager-*.patch"))
HARNESS = Path(__file__).parent / "c/phosh_access_once_harness.c"
DHW = REPO / "tests/daemons-hw/dhw_daemon_bus.py"

STORE_NAME = "org.freedesktop.impl.portal.PermissionStore"
STORE_PATH = "/org/freedesktop/impl/portal/PermissionStore"
STORE_XML = f"""<node><interface name='{STORE_NAME}'>
  <method name='Lookup'><arg direction='in' type='s'/><arg direction='in' type='s'/>
    <arg direction='out' type='a{{sas}}'/><arg direction='out' type='v'/></method>
  <method name='DeletePermission'><arg direction='in' type='s'/><arg direction='in' type='s'/>
    <arg direction='in' type='s'/></method>
  <signal name='Changed'><arg type='s'/><arg type='s'/><arg type='b'/><arg type='v'/>
    <arg type='a{{sas}}'/></signal>
</interface></node>"""
# The tables xdg-desktop-portal 1.22 writes after its access dialog, in the order looked up
WATCHED = [("devices", "camera"), ("location", "location"), ("screenshot", "screenshot"),
           ("wallpaper", "wallpaper")]
APP = "org.gnome.Snapshot"
WINDOW_MS = 1500


def new_file(patch, name):
    """A file the patch adds, as it is after the patch"""
    part = patch.read_text().split(f"+++ b/{name}\n", 1)[1].split("\ndiff --git", 1)[0]
    lines = part.splitlines()[1:]  # the hunk header
    assert all(line.startswith("+") for line in lines), name
    return "\n".join(line[1:] for line in lines) + "\n"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("access-once")
    for name in ("access-once.c", "access-once.h"):
        (d / name).write_text(new_file(PATCH, f"src/{name}"))
    return compile_c(d / "harness", [HARNESS],
                     flags=[f"-I{d}", f'-DACCESS_ONCE_SOURCE="{d / "access-once.c"}"',
                            f"-DACCESS_ONCE_WINDOW_MS={WINDOW_MS}"],
                     pkgs=["gio-unix-2.0"])


@pytest.fixture
def dhw():
    pytest.importorskip("gi")
    return load_script(DHW, "desk_dhw_daemon_bus")


class Store:
    """xdg-permission-store as far as access-once.c uses it, in a thread of its own. `entries`:
    (table, id) -> {app: [permissions]}; an id without entry answers NotFound."""

    def __init__(self, bus, entries=None, on_lookup=None, delete_fails=False):
        from gi.repository import Gio, GLib
        self.GLib = GLib
        self.entries = entries or {}
        self.on_lookup = on_lookup or (lambda table, id_: None)
        self.delete_fails = delete_fails
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
        if method == "Lookup":
            self.on_lookup(*args)
            perms = self.entries.get(args)
            if perms is None:
                invocation.return_dbus_error("org.freedesktop.portal.Error.NotFound", f"No entry for {args[1]}")
            else:
                invocation.return_value(self.GLib.Variant("(a{sas}v)", (perms, self.GLib.Variant("y", 0))))
        elif self.delete_fails:
            invocation.return_dbus_error("org.freedesktop.portal.Error.NotFound", "No entry")
        else:
            invocation.return_value(None)

    def signal(self, table, id_, perms, deleted=False, conn=None, body=None):
        """Changed as the store emits it (or another connection, `conn`)"""
        GLib = self.GLib
        params = body or GLib.Variant("(ssbva{sas})", (table, id_, deleted, GLib.Variant("y", 0), perms))
        done = threading.Event()

        def emit():
            (conn or self.conn).emit_signal(None, STORE_PATH, STORE_NAME, "Changed", params)
            (conn or self.conn).flush_sync(None)
            done.set()
            return False

        self.ctx.invoke_full(0, emit)
        assert done.wait(10)

    def emit_now(self, table, id_, perms):
        """Changed from within a call of the store's thread (before its reply)"""
        GLib = self.GLib
        self.conn.emit_signal(None, STORE_PATH, STORE_NAME, "Changed",
                              GLib.Variant("(ssbva{sas})", (table, id_, False, GLib.Variant("y", 0), perms)))

    def deleted(self):
        return [c[1] for c in self.calls if c[0] == "DeletePermission"]

    def lookups(self):
        return [c[1] for c in self.calls if c[0] == "Lookup"]

    def close(self):
        self.ctx.invoke_full(0, lambda: (self.conn.close_sync(None), self.loop.quit(), False)[-1])
        self.thread.join(10)


class World:
    def __init__(self, dhw, harness, tmp_path, **store):
        self.dhw = dhw
        (tmp_path / "bus").mkdir(parents=True)
        self.bus = dhw.PrivateBus(tmp_path / "bus")
        self.store = Store(self.bus, **store) if store.get("entries", True) is not None else None
        env = dhw.sanitized_env(DBUS_SESSION_BUS_ADDRESS=self.bus.address, G_MESSAGES_DEBUG="all",
                                G_DEBUG="fatal-criticals")
        self.harness = dhw.Daemon([harness], env)
        dhw.poll(lambda: "ready\n" in self.harness.out, what="the harness")

    def debug(self, text, timeout=10):
        """Wait for a debug message (GLib writes them to stdout)"""
        self.dhw.poll(lambda: any(text in line for line in self.harness.out), timeout=timeout, what=text)

    def ask(self, choice, app=APP, **options):
        from gi.repository import GLib
        opts = {k.replace("_", "-"): v for k, v in options.items()}
        return self.bus.call("org.plinius.Test", "/test", "org.plinius.Test", "Ask",
                             GLib.Variant("(sa{sv}s)", (app, opts, choice)), "(ua{sv})")

    def buttons(self, **options):
        from gi.repository import GLib
        opts = {k.replace("_", "-"): v for k, v in options.items()}
        return self.bus.call("org.plinius.Test", "/test", "org.plinius.Test", "Buttons",
                             GLib.Variant("(a{sv})", (opts,)), "(u)")[0]

    def close(self):
        code, stderr, stdout = self.harness.stop()
        if self.store:
            self.store.close()
        self.bus.close()
        assert code == 0 and "exit" in stdout, stderr
        return stderr


@pytest.fixture
def world(dhw, harness, tmp_path):
    worlds = []

    def make(**store):
        w = World(dhw, harness, tmp_path / str(len(worlds)), **store)
        worlds.append(w)
        return w

    yield make
    for w in worlds:
        w.stderr = w.close()


def variant(v):
    from gi.repository import GLib
    return GLib.Variant("as", v)


ONCE, ALWAYS, NEVER = 1, 2, 4


@pytest.mark.parametrize("names,buttons", [
    (None, ONCE | ALWAYS | NEVER),               # xdg-desktop-portal: all three
    (["once", "never"], ONCE | NEVER),           # the session of a user who is not an administrator
    (["always"], ALWAYS),
    (["never", "once", "once"], ONCE | NEVER),   # the order is the dialog's own
    (["maybe"], ONCE | ALWAYS | NEVER),          # nothing known: all three
    ([], ONCE | ALWAYS | NEVER),
])
def test_the_buttons_a_caller_names(world, names, buttons):
    w = world()
    assert w.buttons(**({} if names is None else {"plinius_buttons": variant(names)})) == buttons


def test_buttons_of_the_wrong_type_are_all_three(world):
    from gi.repository import GLib
    assert world().buttons(plinius_buttons=GLib.Variant("s", "once")) == ONCE | ALWAYS | NEVER


@pytest.mark.parametrize("choice,response", [("always", 0), ("never", 1)])
def test_always_and_never_are_kept_as_the_portal_stores_them(world, choice, response):
    w = world()
    assert w.ask(choice) == (response, {"choices": [], "plinius-choice": choice})
    w.store.signal("devices", "camera", {APP: ["yes" if response == 0 else "no"]})
    time.sleep(0.3)
    assert w.store.calls == []


@pytest.mark.parametrize("choice,response,table,value", [
    ("once", 0, ("devices", "camera"), ["yes"]),
    ("once", 0, ("location", "location"), ["EXACT", "123456"]),
    ("once", 0, ("screenshot", "screenshot"), ["yes"]),
    ("once", 0, ("wallpaper", "wallpaper"), ["yes"]),
    # Swiped away: denied this time, asked again next time
    ("cancel", 1, ("devices", "camera"), ["no"]),
    ("cancel", 1, ("location", "location"), ["NONE", "0"]),
])
def test_once_deletes_the_entry_the_portal_writes_for_it(world, choice, response, table, value):
    w = world(entries={("devices", "camera"): {"org.example.Other": ["yes"]}})
    assert w.ask(choice) == (response, {"choices": [], "plinius-choice": choice})
    # Looked up before the answer, every table once
    assert w.store.lookups() == WATCHED
    w.store.signal(*table, {APP: value, "org.example.Other": ["yes"]})
    w.dhw.poll(lambda: w.store.deleted(), what="the deletion")
    # Only the first; later changes are left alone
    w.store.signal(*table, {APP: value})
    time.sleep(0.3)
    assert w.store.deleted() == [(*table, APP)]
    w.debug(f"Allowed once, forgot {table[0]}/{table[1]} of {APP}")


def test_an_entry_the_app_had_already_stays(world):
    # The location's time of the last use is written anew on every start: not the dialog's answer
    w = world(entries={("location", "location"): {APP: ["EXACT", "1"]}})
    assert w.ask("once")[0] == 0
    w.store.signal("location", "location", {APP: ["EXACT", "2"]})
    w.store.signal("devices", "camera", {APP: ["yes"]})
    w.dhw.poll(lambda: w.store.deleted(), what="the deletion")
    assert w.store.deleted() == [("devices", "camera", APP)]


@pytest.mark.parametrize("change", [
    ("notifications", "notification", {APP: ["yes"]}, False),   # a table the dialog never writes
    ("devices", "microphone", {APP: ["yes"]}, False),
    ("devices", "camera", {"org.example.Other": ["yes"]}, False),  # another app
    ("devices", "camera", {}, True),                             # an entry deleted
])
def test_changes_that_are_not_the_answer(world, change):
    w = world()
    w.ask("once")
    table, id_, perms, deleted = change
    w.store.signal(table, id_, perms, deleted=deleted)
    time.sleep(0.3)
    assert w.store.deleted() == []
    w.store.signal("devices", "camera", {APP: ["yes"]})
    w.dhw.poll(lambda: w.store.deleted(), what="the deletion")


def test_a_changed_signal_with_another_body_is_ignored(world):
    from gi.repository import GLib
    w = world()
    w.ask("once")
    w.store.signal(None, None, None, body=GLib.Variant("(ss)", ("devices", "camera")))
    w.store.signal("devices", "camera", {APP: ["yes"]})
    w.dhw.poll(lambda: w.store.deleted(), what="the deletion")
    assert w.store.deleted() == [("devices", "camera", APP)]


def test_only_the_permission_store_s_own_signal_counts(world):
    w = world()
    w.ask("once")
    other = w.bus.connect()
    try:
        w.store.signal("devices", "camera", {APP: ["yes"]}, conn=other)
        time.sleep(0.3)
        assert w.store.deleted() == []
    finally:
        other.close_sync(None)


def test_a_change_while_looking_up_is_no_answer(world):
    # Changed before the reply of the first lookup: the entry is not the dialog's
    def on_lookup(table, id_):
        if table == "devices":
            w.store.emit_now("devices", "camera", {APP: ["yes"]})

    w = world(on_lookup=on_lookup)
    w.ask("once")
    w.store.signal("devices", "camera", {APP: ["yes"]})
    w.store.signal("screenshot", "screenshot", {APP: ["yes"]})
    w.dhw.poll(lambda: w.store.deleted(), what="the deletion")
    assert w.store.deleted() == [("screenshot", "screenshot", APP)]


def test_nothing_is_deleted_after_the_window(world):
    w = world()
    w.ask("once")
    w.debug(f"No new permission of {APP} to forget", timeout=WINDOW_MS / 1000 + 10)
    w.store.signal("devices", "camera", {APP: ["yes"]})
    time.sleep(0.3)
    assert w.store.deleted() == []


def test_a_caller_that_handles_once_itself_is_left_alone(world):
    from gi.repository import GLib
    w = world()
    assert w.ask("once", plinius_once=GLib.Variant("b", True)) == (0, {"choices": [], "plinius-choice": "once"})
    w.store.signal("devices", "camera", {APP: ["yes"]})
    time.sleep(0.3)
    assert w.store.calls == []


def test_without_a_permission_store_the_answer_still_comes(world):
    w = world(entries=None)
    assert w.ask("once") == (0, {"choices": [], "plinius-choice": "once"})


def test_a_deletion_the_store_refuses_is_reported(world):
    w = world(delete_fails=True)
    w.ask("once")
    w.store.signal("devices", "camera", {APP: ["yes"]})
    w.harness.wait_for(f"Could not forget devices/camera of {APP}: ")


def test_two_answers_wait_each_for_their_own_app(world):
    w = world()
    w.ask("once", app="org.example.A")
    w.ask("once", app="org.example.B")
    w.store.signal("devices", "camera", {"org.example.B": ["yes"]})
    w.store.signal("location", "location", {"org.example.A": ["EXACT", "5"], "org.example.B": ["EXACT", "5"]})
    w.dhw.poll(lambda: len(w.store.deleted()) == 2, what="both deletions")
    assert sorted(w.store.deleted()) == [("devices", "camera", "org.example.B"),
                                         ("location", "location", "org.example.A")]
