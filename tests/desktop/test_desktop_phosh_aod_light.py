# phosh-plinius 0116: the always-on display has half its light in the dark. While it is shown it
# claims the light sensor on a system bus connection of its own (Phosh's own claim ends when the
# screen blanks, and sensor-proxy-bridge counts claims per connection). The light sensor part of
# aod.c is taken from the patch as it is and compiled into c/phosh_aod_light_harness.c; a fake
# sensor proxy on a private system bus answers GetAll, ClaimLight and ReleaseLight and emits
# PropertiesChanged as sensor-proxy-bridge does.

import queue
import subprocess
import threading
import time
import warnings
from pathlib import Path

import pytest

from conftest import PKG, REPO, compile_c, load_script

PHOSH = PKG / "phosh-plinius"
PATCH = next(PHOSH.glob("0116-aod-*.patch"))
HARNESS = Path(__file__).parent / "c/phosh_aod_light_harness.c"
DHW = REPO / "tests/daemons-hw/dhw_daemon_bus.py"
BRIDGE = PKG / "sensor-proxy-bridge/sensor-proxy-bridge.c"

NAME = PATH = IFACE = None  # set below from the patch
PROXY_XML = """<node><interface name='net.hadess.SensorProxy'>
  <method name='ClaimLight'/><method name='ReleaseLight'/>
  <property name='HasAmbientLight' type='b' access='read'/>
  <property name='LightLevelUnit' type='s' access='read'/>
  <property name='LightLevel' type='d' access='read'/>
</interface></node>"""


def added():
    """The lines 0116 adds to aod.c"""
    part = PATCH.read_text().split("+++ b/src/aod.c\n", 1)[1].split("\ndiff --git", 1)[0]
    return "\n".join(line[1:] for line in part.splitlines() if line.startswith("+"))


def defines():
    code = added()
    start = code.index("/*\n * Dark below AOD_DARK_LUX")
    end = code.index("static gboolean aod_dark;\n") + len("static gboolean aod_dark;\n")
    return code[start:end]


def light_code():
    code = added()
    start = code.index("/* Light: dark is below")
    return code[start:code.index("/* Light end */")]


def define(name):
    for line in defines().splitlines():
        if line.startswith(f"#define {name} "):
            return line.split(None, 2)[2].strip().strip('"')
    raise AssertionError(name)


NAME, PATH, IFACE = (define(n) for n in ("SENSOR_PROXY_NAME", "SENSOR_PROXY_PATH", "SENSOR_PROXY_IFACE"))
DARK, BRIGHT = float(define("AOD_DARK_LUX")), float(define("AOD_BRIGHT_LUX"))
OPACITY = float(define("AOD_DARK_OPACITY"))


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("aod-light")
    (d / "defines.c").write_text(defines())
    (d / "light.c").write_text(light_code())
    return compile_c(d / "harness", [HARNESS],
                     flags=[f'-DAOD_LIGHT_DEFINES="{d / "defines.c"}"',
                            f'-DAOD_LIGHT_SOURCE="{d / "light.c"}"'],
                     pkgs=["gio-2.0"])


@pytest.fixture
def dhw():
    pytest.importorskip("gi")
    return load_script(DHW, "desk_dhw_daemon_bus_aod")


class SensorProxy:
    """sensor-proxy-bridge as far as the always-on display uses it, in a thread of its own: the
    claims per connection, dropped with the connection as the bridge's name watch does"""

    def __init__(self, bus, lux, has_light=True):
        from gi.repository import Gio, GLib
        self.GLib, self.Gio = GLib, Gio
        self.lux = lux
        self.has_light = has_light
        self.calls = []
        self.claims = set()
        self.ctx = GLib.MainContext.new()
        self.loop = GLib.MainLoop.new(self.ctx, False)
        ready = threading.Event()

        def thread():
            self.ctx.push_thread_default()
            self.conn = bus.connect()
            info = Gio.DBusNodeInfo.new_for_xml(PROXY_XML).interfaces[0]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                self.conn.register_object(PATH, info, self._call, None, None)
            self.conn.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
                                       "/org/freedesktop/DBus", None, Gio.DBusSignalFlags.NONE,
                                       self._owner_changed)
            self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "RequestName", GLib.Variant("(su)", (NAME, 0)), GLib.VariantType("(u)"),
                                Gio.DBusCallFlags.NONE, 5000, None)
            ready.set()
            self.loop.run()
            self.ctx.pop_thread_default()

        self.thread = threading.Thread(target=thread, daemon=True)
        self.thread.start()
        assert ready.wait(10)

    def _owner_changed(self, conn, sender, path, iface, signal, params):
        name, _old, new = params.unpack()
        if not new:
            self.claims.discard(name)

    def _call(self, conn, sender, path, iface, method, params, invocation):
        GLib = self.GLib
        self.calls.append((method, sender))
        if method == "GetAll":
            assert params.unpack() == (IFACE,)
            props = {"HasAmbientLight": GLib.Variant("b", self.has_light),
                     "LightLevelUnit": GLib.Variant("s", "lux"),
                     "LightLevel": GLib.Variant("d", self.lux)}
            invocation.return_value(GLib.Variant("(a{sv})", (props,)))
            return
        if method == "ClaimLight":
            self.claims.add(sender)
        elif method == "ReleaseLight":
            self.claims.discard(sender)
        invocation.return_value(None)

    def light(self, lux, conn=None):
        """A new light level, as the bridge's on_light () emits it (or from another connection)"""
        GLib = self.GLib
        self.lux = lux
        params = GLib.Variant("(sa{sv}as)", (IFACE, {"LightLevel": GLib.Variant("d", lux)}, []))
        done = threading.Event()

        def emit():
            (conn or self.conn).emit_signal(None, PATH, "org.freedesktop.DBus.Properties",
                                            "PropertiesChanged", params)
            (conn or self.conn).flush_sync(None)
            done.set()
            return False

        self.ctx.invoke_full(0, emit)
        assert done.wait(10)

    def methods(self):
        return [m for m, _ in self.calls]

    def close(self):
        self.ctx.invoke_full(0, lambda: (self.conn.close_sync(None), self.loop.quit(), False)[-1])
        self.thread.join(10)


class Aod:
    """The harness: commands on stdin, its stdout lines collected"""

    def __init__(self, program, env):
        self.proc = subprocess.Popen([str(program)], env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.out = []
        self.err = []
        for stream, into in ((self.proc.stdout, self.out), (self.proc.stderr, self.err)):
            threading.Thread(target=lambda s=stream, i=into: [i.append(line.rstrip("\n")) for line in s],
                             daemon=True).start()

    def send(self, *commands):
        for command in commands:
            self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

    def opacities(self):
        return [float(line.split()[1]) for line in list(self.out) if line.startswith("opacity ")]

    def quit(self):
        self.send("quit")
        code = self.proc.wait(timeout=20)
        time.sleep(0.05)
        return code


class World:
    def __init__(self, dhw, harness, tmp_path, lux=None, has_light=True):
        self.dhw = dhw
        (tmp_path / "bus").mkdir(parents=True)
        self.bus = dhw.PrivateBus(tmp_path / "bus")
        self.proxy = SensorProxy(self.bus, lux, has_light) if lux is not None else None
        env = dhw.sanitized_env(DBUS_SYSTEM_BUS_ADDRESS=self.bus.address, G_MESSAGES_DEBUG="all",
                                G_DEBUG="fatal-criticals")
        self.aod = Aod(harness, env)
        self.wait(lambda: "ready" in self.aod.out, "the harness")

    def wait(self, predicate, what, timeout=10):
        return self.dhw.poll(predicate, timeout=timeout, what=what)

    def debug(self, text):
        self.wait(lambda: any(text in line for line in list(self.aod.out)), text)

    def shown(self):
        """A new always-on display, once its claim is in"""
        before = len(self.proxy.claims) if self.proxy else 0
        self.aod.send("new")
        if self.proxy:
            self.wait(lambda: len(self.proxy.claims) > before, "the claim")

    def close(self):
        code = self.aod.quit()
        if self.proxy:
            self.proxy.close()
        self.bus.close()
        assert code == 0 and "exit" in self.aod.out, "\n".join(self.aod.err)
        assert not [line for line in self.aod.err if "CRITICAL" in line], self.aod.err


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


def test_dark_is_half_of_the_light():
    # Over black an OLED pixel at opacity a gives about a^2.2 of its light (sRGB's gamma)
    assert OPACITY ** 2.2 == pytest.approx(0.5, abs=0.01)
    assert 0 < DARK < BRIGHT


def test_a_dark_room_dims_the_display(world):
    w = world(lux=1.0)
    w.shown()
    w.wait(lambda: w.aod.opacities() == [1.0, OPACITY], "dimmed")
    w.debug("Ambient light 1.0 lux: dark")
    # The last value first, then the claim; one connection, one claim
    assert w.proxy.methods() == ["GetAll", "ClaimLight"]
    assert len(w.proxy.claims) == 1


def test_a_lit_room_stays_bright(world):
    w = world(lux=300.0)
    w.shown()
    w.debug("Ambient light 300.0 lux: light")
    time.sleep(0.2)
    # Set once at the start, no frame for a value that changes nothing
    assert w.aod.opacities() == [1.0]


def test_the_light_switches_with_a_gap(world):
    w = world(lux=300.0)
    w.shown()
    w.debug("Ambient light 300.0 lux: light")
    steps = [(DARK + 1, 1.0), (DARK - 0.5, OPACITY), (DARK + 1, OPACITY), (BRIGHT - 0.5, OPACITY),
             (BRIGHT, 1.0), (DARK, 1.0), (0.0, OPACITY)]
    for lux, opacity in steps:
        w.proxy.light(lux)
        w.debug(f"Ambient light {lux:.1f} lux")
        assert w.aod.opacities()[-1] == opacity, lux
    # Only the two changes drew a frame
    assert w.aod.opacities() == [1.0, OPACITY, 1.0, OPACITY]


def test_a_new_display_starts_dark_in_the_dark(world):
    # Made anew when the phone locks, or the next one: no bright frame before the sensor answers
    w = world(lux=0.0)
    w.shown()
    w.wait(lambda: w.aod.opacities()[-1:] == [OPACITY], "dimmed")
    w.aod.send("destroy", "new")
    w.wait(lambda: w.aod.opacities() == [1.0, OPACITY, OPACITY], "the second display")
    w.debug("Ambient light 0.0 lux: dark")


def test_hiding_drops_the_claim(world):
    w = world(lux=2.0)
    w.shown()
    claimer, = w.proxy.claims
    w.aod.send("destroy")
    w.wait(lambda: not w.proxy.claims, "the claim dropped")
    from gi.repository import GLib
    owned = w.bus.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                       "NameHasOwner", GLib.Variant("(s)", (claimer,)), "(b)")[0]
    assert not owned
    # A later level reaches nobody
    w.proxy.light(500.0)
    time.sleep(0.2)
    assert w.aod.opacities() == [1.0, OPACITY]
    # The next display claims anew, on a connection of its own
    w.shown()
    assert w.proxy.claims and claimer not in w.proxy.claims


def test_hidden_before_the_bus_answers(world):
    # Shown and hidden again at once (the screen back on): nothing claimed, nothing left behind
    w = world(lux=1.0)
    w.aod.send("new", "destroy")
    w.debug("done destroy")
    time.sleep(0.3)
    assert w.proxy.claims == set()
    assert "ClaimLight" not in w.proxy.methods()


def test_without_a_light_sensor_nothing_changes(world):
    # The bridge without a light sensor still has LightLevel 0
    w = world(lux=0.0, has_light=False)
    w.shown()
    time.sleep(0.2)
    assert w.aod.opacities() == [1.0]


def test_without_the_sensor_proxy_the_display_stays_as_it_is(world):
    w = world()
    w.aod.send("new")
    w.debug("No light sensor")
    w.wait(lambda: any("Failed to claim the light sensor" in line for line in w.aod.err), "the warning")
    assert w.aod.opacities() == [1.0]


def test_only_the_sensor_proxy_is_heard(world):
    w = world(lux=300.0)
    w.shown()
    w.debug("Ambient light 300.0 lux: light")
    other = w.bus.connect()
    try:
        w.proxy.light(0.0, conn=other)
        time.sleep(0.2)
        assert w.aod.opacities() == [1.0]
    finally:
        other.close_sync(None)


def test_the_patch_wires_it_into_the_display():
    code = added()
    text = PATCH.read_text()
    # Claimed once the content box is there, released before the rest goes
    constructed = text.split("phosh_aod_constructed", 1)[1]
    assert constructed.index("gtk_container_add (GTK_CONTAINER (self), self->box);") < \
        constructed.index("+  claim_light (self);")
    assert "+  release_light (self);\n   g_clear_handle_id (&self->update_icons_id" in text
    for field in ("GCancellable          *light_cancel;", "GDBusConnection       *light_bus;",
                  "guint                  light_signal_id;"):
        assert field in code


def test_the_bridge_serves_what_the_display_asks_for():
    # sensor-proxy-bridge has the names, the property and the claim the patch relies on
    bridge = BRIDGE.read_text()
    assert f"<interface name='{IFACE}'>" in bridge
    assert f'emit_changed_on("{PATH}", "{IFACE}", property, value);' in bridge
    assert '"LightLevel", g_variant_new_double(lux)' in bridge
    assert "<method name='ClaimLight'/>" in bridge
    # Claims are per D-Bus name and end when it vanishes
    assert "GHashTable *claims; /* D-Bus unique name -> watch id */" in bridge
