# plinius-send-to-tv: the native messaging host that hands a link from Firefox to the TV Schwupp
# is connected to. The host talks to a stand-in Schwupp on a private dbus-daemon (the real
# interface: ActiveDevice, Devices, Actions, State, Error, PlayLink); Firefox's side of the
# protocol (length-prefixed JSON on stdin/stdout) is driven in-process and, once, as a process.

import io
import json
import struct
import subprocess
import sys
import threading
import warnings

import pytest
from gi.repository import Gio, GLib

from conftest import PKG, REPO, load_script

PKGDIR = PKG / "plinius-send-to-tv"
HOST = PKGDIR / "send-to-tv-host"
bus_helpers = load_script(REPO / "tests" / "daemons-hw" / "dhw_daemon_bus.py", "send_to_tv_bus")

# Schwupp's interface as far as the host uses it (app/service.py of the pinned commit)
XML = """<node><interface name="de.cais.Schwupp.Cast1">
  <method name="PlayLink"><arg name="id" type="s" direction="in"/>
    <arg name="url" type="s" direction="in"/></method>
  <property name="Devices" type="a(ssss)" access="read"/>
  <property name="ActiveDevice" type="s" access="read"/>
  <property name="State" type="s" access="read"/>
  <property name="Error" type="s" access="read"/>
  <property name="Actions" type="a{sas}" access="read"/>
</interface></node>"""

LIVING = ("webos:192.0.2.33", "Living Room", "LG webOS · OLED", "tv-symbolic")
SPEAKER = ("dlna:192.0.2.40", "Kitchen Radio", "Speaker", "audio-speakers-symbolic")
VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture(scope="module")
def mod():
    return load_script(HOST)


class Schwupp:
    """A stand-in for Schwupp's cast service in a thread of its own. `states` are the values
    State takes one after another (the last one stays); PlayLink answers `play_error` as a D-Bus
    error when set, and may switch the active device (`switch_to`)."""

    TYPES = {"Devices": "a(ssss)", "ActiveDevice": "s", "State": "s", "Error": "s",
             "Actions": "a{sas}"}

    def __init__(self, bus):
        self.props = {"Devices": [LIVING, SPEAKER], "ActiveDevice": LIVING[0], "State": "streaming",
                      "Error": "", "Actions": {LIVING[0]: ["mirror", "youtube", "video"],
                                               SPEAKER[0]: []}}
        self.states, self.play_error, self.switch_to, self.calls = [], None, None, []
        self.ctx = GLib.MainContext.new()
        self.loop = GLib.MainLoop.new(self.ctx, False)
        ready = threading.Event()

        def thread():
            self.ctx.push_thread_default()
            self.conn = bus.connect()
            info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                self.conn.register_object("/de/cais/Schwupp/Cast", info, self.on_call, None, None)
            self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                "org.freedesktop.DBus", "RequestName",
                                GLib.Variant("(su)", ("de.cais.Schwupp", 0)), GLib.VariantType("(u)"),
                                Gio.DBusCallFlags.NONE, 5000, None)
            ready.set()
            self.loop.run()
            self.ctx.pop_thread_default()

        self.thread = threading.Thread(target=thread, daemon=True)
        self.thread.start()
        assert ready.wait(10)

    def on_call(self, conn, sender, path, iface, method, params, invocation):
        args = params.unpack()
        self.calls.append((method, args))
        if method == "Get":
            name = args[1]
            if name == "State" and self.states:
                self.props["State"] = self.states.pop(0)
            invocation.return_value(GLib.Variant(
                "(v)", (GLib.Variant(self.TYPES[name], self.props[name]),)))
        elif method == "PlayLink":
            if self.play_error:
                invocation.return_dbus_error(*self.play_error)
                return
            if self.switch_to is not None:
                self.props["ActiveDevice"] = self.switch_to
            invocation.return_value(None)

    def played(self):
        return [args for method, args in self.calls if method == "PlayLink"]

    def close(self):
        self.ctx.invoke_full(0, lambda: (self.conn.close_sync(None), self.loop.quit(), False)[-1])
        self.thread.join(10)


@pytest.fixture
def bus(tmp_path):
    b = bus_helpers.PrivateBus(tmp_path)
    yield b
    b.close()


@pytest.fixture
def schwupp(bus):
    s = Schwupp(bus)
    yield s
    s.close()


@pytest.fixture
def host(mod, bus):
    conn = bus.connect()
    yield mod.Schwupp(conn)
    conn.close_sync(None)


class Clock:
    """Time for wait_for_start: sleep moves it on instead of waiting."""

    def __init__(self):
        self.now, self.sleeps = 100.0, 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps += 1


def play(mod, host, url=VIDEO, clock=None):
    clock = clock or Clock()
    return mod.handle(host, {"action": "play", "url": url}, clock=clock, sleep=clock.sleep)


# ---- status: what the context menu shows ----

def test_status_without_schwupp_starts_nothing(mod, host, bus):
    # No owner of de.cais.Schwupp: not connected, and nothing was asked of Schwupp (the bus has
    # no services to activate, but the host must not even try: it asks NameHasOwner first)
    assert mod.handle(host, {"action": "status"}) == {"connected": False, "name": "",
                                                       "links": False}


def test_status_names_the_connected_tv(mod, host, schwupp):
    assert mod.handle(host, {"action": "status"}) == {"connected": True, "name": "Living Room",
                                                       "links": True}
    # Read only, with NO_AUTO_START: no method of Schwupp is called
    assert {m for m, _ in schwupp.calls} == {"Get"}


def test_status_when_schwupp_runs_but_nothing_is_connected(mod, host, schwupp):
    schwupp.props["ActiveDevice"] = ""
    assert mod.handle(host, {"action": "status"})["connected"] is False


def test_status_of_a_tv_without_links(mod, host, schwupp):
    schwupp.props["Actions"] = {LIVING[0]: ["mirror"]}
    assert mod.handle(host, {"action": "status"}) == {"connected": True, "name": "Living Room",
                                                       "links": False}


def test_status_of_a_device_missing_from_the_list(mod, host, schwupp):
    schwupp.props["Devices"], schwupp.props["Actions"] = [], {}
    assert mod.handle(host, {"action": "status"}) == {"connected": True, "name": "",
                                                       "links": False}


# ---- play ----

def test_play_hands_the_link_to_the_active_tv(mod, host, schwupp):
    schwupp.states = ["connecting", "connecting", "streaming"]
    clock = Clock()
    assert play(mod, host, clock=clock) == {"result": "playing", "name": "Living Room"}
    assert schwupp.played() == [(LIVING[0], VIDEO)]
    assert clock.sleeps == 2


def test_play_reports_the_code_the_tv_asks_for(mod, host, schwupp):
    schwupp.states = ["connecting", "pin"]
    assert play(mod, host) == {"result": "code", "name": "Living Room"}


def test_play_reports_schwupps_error_when_the_tv_fails(mod, host, schwupp):
    schwupp.states = ["connecting", "idle"]
    schwupp.props["Error"] = "The TV did not answer"
    assert play(mod, host) == {"result": "error", "error": "failed", "name": "Living Room",
                               "detail": "The TV did not answer"}


def test_play_ends_when_another_request_takes_the_tv(mod, host, schwupp):
    schwupp.states = ["connecting"]
    schwupp.switch_to = SPEAKER[0]
    assert play(mod, host)["error"] == "failed"


def test_play_stops_waiting_after_a_while(mod, host, schwupp):
    schwupp.states = ["connecting"]
    clock = Clock()
    assert play(mod, host, clock=clock) == {"result": "starting", "name": "Living Room"}
    assert clock.now >= 100.0 + mod.WAIT_S
    assert clock.sleeps == pytest.approx(mod.WAIT_S / mod.POLL_S, abs=1)


def test_play_passes_on_why_schwupp_refused(mod, host, schwupp):
    schwupp.play_error = ("org.freedesktop.DBus.Error.InvalidArgs", "No link for this TV")
    assert play(mod, host) == {"result": "error", "error": "failed", "name": "Living Room",
                               "detail": "No link for this TV"}


def test_play_without_a_connection(mod, host, schwupp):
    schwupp.props["ActiveDevice"] = ""
    assert play(mod, host) == {"result": "error", "error": "no-tv", "name": "", "detail": ""}
    assert schwupp.played() == []


def test_play_without_schwupp(mod, host):
    assert play(mod, host)["error"] == "no-tv"


def test_play_on_a_tv_that_cannot_play_links(mod, host, schwupp):
    schwupp.props["Actions"] = {LIVING[0]: ["mirror"]}
    assert play(mod, host) == {"result": "error", "error": "no-links", "name": "Living Room",
                               "detail": ""}
    assert schwupp.played() == []


@pytest.mark.parametrize("url", [
    "", None, 42, "javascript:alert(1)", "file:///etc/passwd", "youtube.com/watch?v=x",
    "https://www.youtube.com/watch?v=a b", "https://www.youtube.com/watch?v=a\nb",
    "https://www.youtube.com/watch?v=" + "x" * 5000,
])
def test_play_refuses_what_is_no_web_link(mod, host, schwupp, url):
    assert play(mod, host, url=url)["error"] == "bad-url"
    assert schwupp.calls == []


def test_play_accepts_plain_http(mod):
    assert mod.valid_url("http://youtu.be/dQw4w9WgXcQ")


# ---- requests and the bus going away ----

@pytest.mark.parametrize("request_", [[], "status", {}, {"action": "stop"}])
def test_unknown_requests(mod, host, request_):
    assert mod.handle(host, request_)["error"] == "bad-request"


class Gone:
    """Schwupp left the bus between NameHasOwner and the next call."""

    def running(self):
        return True

    def get(self, name):
        raise GLib.Error("GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown: gone")


def test_schwupp_leaving_in_between(mod):
    assert mod.handle(Gone(), {"action": "status"}) == {"connected": False, "name": "",
                                                         "links": False}
    reply = mod.handle(Gone(), {"action": "play", "url": VIDEO})
    assert reply["result"] == "error" and reply["error"] == "failed" and "gone" in reply["detail"]


# ---- Firefox's protocol ----

def frame(message):
    body = json.dumps(message).encode()
    return struct.pack("=I", len(body)) + body


def unframe(data):
    out = []
    while data:
        (n,) = struct.unpack("=I", data[:4])
        out.append(json.loads(data[4:4 + n]))
        data = data[4 + n:]
    return out


def test_serve_answers_each_message_until_the_end(mod, host, schwupp):
    stdin = io.BytesIO(frame({"action": "status"}) + frame({"action": "nope"}))
    stdout = io.BytesIO()
    assert mod.serve(host, stdin, stdout) == 0
    assert unframe(stdout.getvalue()) == [
        {"connected": True, "name": "Living Room", "links": True},
        {"result": "error", "error": "bad-request", "name": "", "detail": ""}]


@pytest.mark.parametrize("data", [
    b"\x01\x02",                                          # cut off in the length
    struct.pack("=I", 10) + b"{}",                       # cut off in the body
])
def test_serve_stops_quietly_on_cut_input(mod, host, data):
    stdout = io.BytesIO()
    assert mod.serve(host, io.BytesIO(data), stdout) == 0
    assert stdout.getvalue() == b""


@pytest.mark.parametrize("data", [
    struct.pack("=I", 2 * 1024 * 1024),                   # longer than Firefox may send
    struct.pack("=I", 3) + b"{x}",                        # no JSON
    struct.pack("=I", 2) + b"\xff\xfe",                   # no UTF-8
])
def test_serve_answers_once_and_stops_on_garbage(mod, host, data):
    stdout = io.BytesIO()
    assert mod.serve(host, io.BytesIO(data), stdout) == 1
    assert unframe(stdout.getvalue()) == [
        {"result": "error", "error": "bad-request", "name": "", "detail": ""}]


def test_main_without_a_session_bus(mod, monkeypatch):
    # conftest points the session bus at nothing: no Schwupp, so nothing is connected
    stdin, stdout = io.BytesIO(frame({"action": "status"})), io.BytesIO()
    monkeypatch.setattr(sys, "stdin", type("In", (), {"buffer": stdin})())
    monkeypatch.setattr(sys, "stdout", type("Out", (), {"buffer": stdout})())
    assert mod.main() == 0
    assert unframe(stdout.getvalue()) == [{"connected": False, "name": "", "links": False}]
    # and a play request is refused the same way
    stdin2, stdout2 = io.BytesIO(frame({"action": "play", "url": VIDEO})), io.BytesIO()
    monkeypatch.setattr(sys, "stdin", type("In", (), {"buffer": stdin2})())
    monkeypatch.setattr(sys, "stdout", type("Out", (), {"buffer": stdout2})())
    assert mod.main() == 0
    assert unframe(stdout2.getvalue())[0]["error"] == "no-tv"


def test_main_on_the_session_bus(mod, schwupp, bus, monkeypatch):
    monkeypatch.setattr(mod.Gio, "bus_get_sync", lambda kind, cancel: bus.connect())
    stdin, stdout = io.BytesIO(frame({"action": "status"})), io.BytesIO()
    monkeypatch.setattr(sys, "stdin", type("In", (), {"buffer": stdin})())
    monkeypatch.setattr(sys, "stdout", type("Out", (), {"buffer": stdout})())
    assert mod.main() == 0
    assert unframe(stdout.getvalue())[0]["name"] == "Living Room"


def test_the_host_as_firefox_starts_it(schwupp, bus):
    # As Firefox does: the program with the manifest's name as argument, the bus from the
    # environment; one message, then the end of input
    r = subprocess.run([sys.executable, "-I", str(HOST), "/usr/lib/mozilla/native-messaging-hosts/"
                        "de.cais.send_to_tv.json", "send-to-tv@de.cais"],
                       input=frame({"action": "play", "url": VIDEO}), capture_output=True,
                       timeout=60, env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent",
                                        "DBUS_SESSION_BUS_ADDRESS": bus.address})
    assert r.returncode == 0, r.stderr.decode()
    assert unframe(r.stdout) == [{"result": "playing", "name": "Living Room"}]
    assert schwupp.played() == [(LIVING[0], VIDEO)]


def test_shebang_isolates_python():
    first = HOST.read_text().splitlines()[0]
    assert first == "#!/usr/bin/python3 -I"


def test_errors_without_a_dbus_name_keep_their_text(mod):
    assert mod.remote_text(GLib.Error("Timeout was reached")) == "Timeout was reached"
