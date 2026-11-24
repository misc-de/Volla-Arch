# plinius-desktop-session: the scale that keeps the desktop's layout, the event loop of one VNC
# session and of the desktop on a TV (--cast), sharing phoc's one virtual screen between them,
# capturing the new screen, the bar, the standby inhibitor and the teardown. The
# Session object is made without __init__ (that one connects to the buses); its programs, buses
# and clock are fakes.

import json
import os
import queue
import re
import subprocess
import types

import pytest
from gi.repository import GLib

import desktop_fakes as fakes
from conftest import PKG, load_script

DESK = PKG / "plinius-desktop"


@pytest.fixture(scope="module")
def mod(monkeypatch_module):
    monkeypatch_module.syspath_prepend(str(DESK))
    load_script(DESK / "desk_common.py", "desk_common")
    return load_script(DESK / "plinius-desktop-session", "plinius_desktop_session_under_test")


@pytest.fixture(scope="module")
def monkeypatch_module():
    with pytest.MonkeyPatch.context() as mp:
        yield mp


class Stopping:
    """threading.Event whose wait() answers from a list instead of waiting."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.flag = False
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.answers:
            return self.answers.pop(0)
        return True

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True


@pytest.fixture
def session(mod, tmp_path, monkeypatch):
    common = mod.common
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setattr(common, "RUNTIME_DIR", str(run))
    screen = tmp_path / "screen"
    monkeypatch.setattr(common, "SCREEN_DIR", str(screen))
    monkeypatch.setattr(common, "OUTPUT_FILE", str(screen / "output"))
    monkeypatch.setattr(common, "CLIENT_FILE", str(run / "client"))
    monkeypatch.setattr(common, "CTL_SOCKET", str(run / "wayvnc.sock"))
    s = mod.Session.__new__(mod.Session)
    s.events = queue.Queue()
    s.bus = s.system_bus = None
    s.wayvnc = s.waybar = s.apps_menu = s.output = s.inhibitor = None
    s.notification = 0
    s.mode, s.owner = "vnc", False
    s.stopping = Stopping()
    s.run_dir = run
    return s


# ----- wanted_scale -----

@pytest.mark.parametrize("mode, settings, scale", [
    ("1920x1080", {"resolution": "1920x1080", "scale": "1.5"}, 1.5),
    ("3840x2160", {"resolution": "1920x1080", "scale": "1.5"}, 3.0),
    ("960x540", {"resolution": "1920x1080", "scale": "1"}, 0.5),
    # A window of another shape: the side that fits less decides, nothing is cut off
    ("3840x1080", {"resolution": "1920x1080", "scale": "1.25"}, 1.25),
    ("1366x768", {"resolution": "1280x720", "scale": "1.75"}, round(1.75 * 768 / 720, 3)),
])
def test_wanted_scale(mod, mode, settings, scale):
    assert mod.wanted_scale(mode, settings) == scale


@pytest.mark.parametrize("mode, settings", [
    (None, {"resolution": "1920x1080", "scale": "1"}),
    ("1920x1080", {"resolution": "big", "scale": "1"}),
    ("1920x1080", {"resolution": "1920x1080", "scale": "x"}),
    ("19201080", {"resolution": "1920x1080", "scale": "1"}),
])
def test_wanted_scale_of_nonsense_is_none(mod, mode, settings):
    assert mod.wanted_scale(mode, settings) is None


# ----- keep_layout -----

def test_keep_layout_rescales_only_on_a_change(session, mod, monkeypatch):
    common = mod.common
    session.output = "HEADLESS-1"
    states = iter([
        {"HEADLESS-1": {"enabled": True, "mode": "3840x2160", "scale": 1.5}},
        {"HEADLESS-1": {"enabled": True, "mode": "3840x2160", "scale": 3.0}},  # applied
        {"HEADLESS-1": {"enabled": True, "mode": "3840x2160", "scale": 3.005}},  # noise
        {"HEADLESS-1": {"enabled": False, "mode": "1280x720", "scale": 1.0}},
        {},
    ])
    monkeypatch.setattr(common, "outputs", lambda: next(states))
    monkeypatch.setattr(common, "load", lambda: {"resolution": "1920x1080", "scale": "1.5"})
    runs = []
    monkeypatch.setattr(common, "run", runs.append)
    session.stopping = Stopping([False] * 5 + [True])
    session.keep_layout()
    assert runs == [["wlr-randr", "--output", "HEADLESS-1", "--scale", "3.0"]]
    assert session.stopping.waits == [mod.LAYOUT_POLL] * 6


# ----- capturing the new screen -----

def test_capture_screen_retries_until_wayvnc_knows_the_screen(session, mod, monkeypatch):
    session.output = "HEADLESS-3"
    answers = [1, 1, 0]
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, answers.pop(0), "No such output", "")
    monkeypatch.setattr(mod.subprocess, "run", run)
    session.stopping = Stopping([False, False])
    assert session.capture_screen() is True
    assert calls == [["wayvncctl", "-S", mod.common.CTL_SOCKET, "output-set", "HEADLESS-3"]] * 3
    assert session.stopping.waits == [mod.SWITCH_RETRY] * 2


def test_capture_screen_gives_up_at_the_deadline(session, mod, monkeypatch):
    session.output = "HEADLESS-3"
    clock = iter([100.0, 101.0, 106.0])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, "No such output", ""))
    session.stopping = Stopping([False])
    assert session.capture_screen() is False
    assert session.stopping.waits == [mod.SWITCH_RETRY]


def test_capture_screen_stops_with_the_session(session, mod, monkeypatch):
    session.output = "HEADLESS-3"
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, "No such output", ""))
    session.stopping = Stopping([True])
    assert session.capture_screen() is False


# ----- screen_on -----

class FakeVariantReply:
    def __init__(self, value):
        self.value = value

    def unpack(self):
        return (self.value,)


def test_screen_on_creates_the_screen_from_the_settings(session, mod, monkeypatch):
    common = mod.common
    calls = []

    def virtual_call(method, args, reply=None):
        calls.append((method, args.unpack(), reply))
        return FakeVariantReply("HEADLESS-7")
    session.virtual_call = virtual_call
    session.capture_screen = lambda: True
    session.start_bar = lambda: calls.append("bar")
    monkeypatch.setattr(common, "load", lambda: {"resolution": "2560x1440", "scale": "1.25",
                                                 "sound": "desktop", "keyboard": "us"})
    monkeypatch.setattr(common, "set_sound", lambda t: calls.append(("sound", t)))
    started = []
    monkeypatch.setattr(mod.threading, "Thread",
                        lambda target, daemon: type("T", (), {"start": lambda s: started.append(
                            target.__name__)})())
    session.screen_on()
    assert calls[0] == ("Create", (2560, 1440, 1.25, 1100, 0), "(s)")
    assert calls[1:] == ["bar", ("sound", "desktop")]
    with open(common.OUTPUT_FILE) as f:
        assert f.read() == "HEADLESS-7"
    assert started == ["watch_bar_position", "keep_layout", "keep_awake"]


def test_screen_on_stops_when_the_screen_cannot_be_captured(session, mod, monkeypatch):
    session.virtual_call = lambda *a, **k: FakeVariantReply("HEADLESS-7")
    session.capture_screen = lambda: False
    session.start_bar = lambda: pytest.fail("no bar without a picture")
    monkeypatch.setattr(mod.common, "load", lambda: {"resolution": "1920x1080", "scale": "1",
                                                     "sound": "phone", "keyboard": "us"})
    session.screen_on()
    assert session.events.get_nowait() == {"method": "stop"}


# ----- the bar -----

def test_start_bar_fills_in_output_and_edge(session, mod, monkeypatch):
    monkeypatch.setattr(mod, "WAYBAR_TEMPLATE", str(DESK / "waybar.jsonc"))
    monkeypatch.setattr(mod.common, "bar_position", lambda: "bottom")
    started = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda cmd: started.append(cmd) or cmd[0])
    session.output = "HEADLESS-4"
    os.makedirs(mod.common.SCREEN_DIR)
    session.start_bar()
    # Next to the screen's name, not in the VNC session's directory
    path = os.path.join(mod.common.SCREEN_DIR, "waybar.jsonc")
    # The app menu waits hidden from the start, its button only shows it
    assert started == [["waybar", "-c", path, "-s", mod.WAYBAR_STYLE],
                       [mod.APPS_MENU, "--resident"]]
    assert session.waybar == "waybar" and session.apps_menu == mod.APPS_MENU
    text = open(path).read()
    # waybar takes JSON with // comments
    config = json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M))
    assert config["output"] == "HEADLESS-4" and config["position"] == "bottom"
    # A click on a window in the task bar brings it to the front, or minimizes it when it is
    assert config["wlr/taskbar"]["on-click"] == "minimize-raise"
    assert "@" not in text
    # Over VNC the clock shows minutes
    assert config["clock"]["format"] == "{:%a %d %b  %H:%M}" and "interval" not in config["clock"]


def test_on_a_tv_the_clock_shows_seconds(session, mod, monkeypatch):
    """The screencast only gets a frame when the screen changes: an idle desktop on the TV
    stayed black until the minute changed."""
    monkeypatch.setattr(mod, "WAYBAR_TEMPLATE", str(DESK / "waybar.jsonc"))
    monkeypatch.setattr(mod.common, "bar_position", lambda: "top")
    monkeypatch.setattr(mod.subprocess, "Popen", lambda cmd: cmd[0])
    session.output, session.mode = "HEADLESS-4", "cast"
    os.makedirs(mod.common.SCREEN_DIR)
    session.start_bar()
    text = open(os.path.join(mod.common.SCREEN_DIR, "waybar.jsonc")).read()
    clock = json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M))["clock"]
    assert clock == {"format": "{:%a %d %b  %H:%M:%S}", "interval": 1, "tooltip": False}


def test_move_bar_restarts_waybar_and_closes_the_menus(session, mod, monkeypatch):
    events = []

    class Child:
        def __init__(self, name, hangs):
            self.name, self.hangs = name, hangs

        def terminate(self):
            events.append(("terminate", self.name))

        def wait(self, timeout=None):
            events.append(("wait", self.name, timeout))
            if timeout and self.hangs:
                raise subprocess.TimeoutExpired(self.name, timeout)

        def kill(self):
            events.append(("kill", self.name))
    session.waybar = Child("waybar", hangs=True)
    session.apps_menu = Child("apps", hangs=False)
    monkeypatch.setattr(mod.common, "close_all_menus", lambda: events.append("menus"))
    session.start_bar = lambda: events.append("start")
    session.move_bar()
    assert events == ["menus", ("terminate", "waybar"), ("wait", "waybar", 3), ("kill", "waybar"),
                      ("wait", "waybar", None), ("terminate", "apps"), ("wait", "apps", 3),
                      "start"]


def test_move_bar_without_an_app_menu(session, mod, monkeypatch):
    events = []
    session.waybar = types.SimpleNamespace(terminate=lambda: events.append("terminate"),
                                           wait=lambda timeout=None: events.append("wait"))
    monkeypatch.setattr(mod.common, "close_all_menus", lambda: None)
    session.start_bar = lambda: events.append("start")
    session.move_bar()
    assert events == ["terminate", "wait", "start"]


def test_move_bar_does_nothing_while_stopping(session, mod, monkeypatch):
    session.waybar = object()
    session.stopping.set()
    session.start_bar = lambda: pytest.fail("no bar while stopping")
    session.move_bar()


# ----- the event loop -----

@pytest.fixture
def loop(session, mod, monkeypatch):
    """run() with wayvnc, the screen and the notification faked; no grace time to wait out."""
    monkeypatch.setattr(mod, "LOGIN_WAIT", 0.0)
    monkeypatch.setattr(mod, "GRACE", 0.0)
    monkeypatch.setattr(mod.signal, "signal", lambda *a: None)
    session.log = []
    session.start_wayvnc = lambda: session.log.append("wayvnc")

    def screen_on():
        session.log.append("screen")
        session.output = "HEADLESS-1"
    session.screen_on = screen_on
    session.notify = lambda address: session.log.append(("notify", address))
    session.move_bar = lambda: session.log.append("bar-moved")
    return session


def put(session, *events):
    for e in events:
        session.events.put(e)


def test_run_ends_without_a_login(loop):
    loop.run()
    assert loop.log == ["wayvnc"]


def test_run_brings_the_screen_up_once(loop, mod):
    put(loop, {"method": "client-connected", "params": {"address": "::ffff:10.1.2.3"}},
        {"method": "client-connected", "params": {"address": "10.9.9.9"}},
        {"method": "bar-moved"},
        {"method": "client-disconnected", "params": {"connection_count": 1}},
        {"method": "stop"})
    loop.run()
    assert loop.log == ["wayvnc", "screen", ("notify", "10.1.2.3"), "bar-moved"]
    with open(mod.common.CLIENT_FILE) as f:
        assert f.read() == "10.1.2.3"


def test_run_ends_after_the_last_viewer_left(loop):
    put(loop, {"method": "client-connected", "params": {}},
        {"method": "client-disconnected", "params": {"connection_count": 0}})
    loop.run()
    assert loop.log == ["wayvnc", "screen", ("notify", None)]


def test_run_ends_when_wayvnc_exits(loop):
    put(loop, {"method": "client-connected", "params": {"address": "10.0.0.1"}},
        {"method": "wayvnc-exited"}, {"method": "client-connected"})
    loop.run()
    assert loop.log == ["wayvnc", "screen", ("notify", "10.0.0.1")]


def test_start_wayvnc_needs_the_socket(session, monkeypatch):
    monkeypatch.delenv("LISTEN_FDS", raising=False)
    with pytest.raises(SystemExit):
        session.start_wayvnc()


def test_write_config_requires_login_and_encryption(session, mod):
    path = session.write_config()
    assert os.path.dirname(path) == mod.common.RUNTIME_DIR
    lines = open(path).read().splitlines()
    assert lines[:2] == ["enable_auth=true", "enable_pam=true"]
    keys = dict(line.split("=", 1) for line in lines)
    assert keys["private_key_file"].endswith("/plinius-desktop/tls_key.pem")
    assert keys["certificate_file"].endswith("/plinius-desktop/tls_cert.pem")
    assert keys["rsa_private_key_file"].endswith("/plinius-desktop/rsa_key.pem")


# ----- standby -----

class SystemBus:
    def __init__(self, on_battery):
        self.on_battery = list(on_battery)
        self.inhibits = 0
        self.fds = []

    def call_sync(self, name, path, iface, method, params, reply, flags, timeout, cancel):
        assert (name, method) == ("org.freedesktop.UPower", "Get")
        value = self.on_battery.pop(0)
        if value is None:
            raise GLib.Error("no UPower")
        return GLib.Variant("(v)", (GLib.Variant("b", value),))

    def call_with_unix_fd_list_sync(self, name, path, iface, method, params, reply, flags,
                                    timeout, fd_list, cancel):
        assert (method, params.unpack()[0], params.unpack()[3]) == ("Inhibit", "sleep", "block")
        self.inhibits += 1
        r, w = os.pipe()
        os.close(w)
        self.fds.append(r)
        return GLib.Variant("(h)", (0,)), type("L", (), {"get": lambda s, i: r})()


def fd_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


@pytest.mark.parametrize("answer, expected", [(True, True), (False, False), (None, True)])
def test_on_battery(session, answer, expected):
    session.system_bus = SystemBus([answer])
    assert session.on_battery() is expected


def test_keep_awake_follows_the_power_supply(session):
    # Plugged in, still plugged in, on battery, plugged in again, then the session ends
    session.system_bus = bus = SystemBus([False, False, True, False])
    session.stopping = Stopping([False, False, False, True])
    seen = []
    real_wait = session.stopping.wait

    def wait(timeout=None):
        seen.append(session.inhibitor)
        return real_wait(timeout)
    session.stopping.wait = wait
    session.keep_awake()
    assert bus.inhibits == 2
    assert seen[0] == seen[1] == bus.fds[0] and seen[2] is None and seen[3] == bus.fds[1]
    # Both inhibitor fds are closed again, the second one when the session ended
    assert not fd_open(bus.fds[0]) and not fd_open(bus.fds[1])
    assert session.inhibitor is None


def test_keep_awake_on_battery_takes_no_inhibitor(session):
    session.system_bus = bus = SystemBus([True])
    session.stopping = Stopping([True])
    session.keep_awake()
    assert bus.inhibits == 0 and session.inhibitor is None


# ----- teardown -----

class Process:
    def __init__(self, log, name, running=True, hangs=False):
        self.log, self.name, self.running, self.hangs = log, name, running, hangs

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.log.append(("terminate", self.name))

    def wait(self, timeout=None):
        if self.hangs:
            raise subprocess.TimeoutExpired(self.name, timeout)

    def kill(self):
        self.log.append(("kill", self.name))


def test_stop_undoes_everything_in_reverse(session, mod, monkeypatch):
    log = []
    session.waybar = Process(log, "waybar", hangs=True)
    session.apps_menu = Process(log, "apps")
    session.wayvnc = Process(log, "wayvnc", running=False)
    session.events_process = Process(log, "events")
    session.output = "HEADLESS-1"
    session.owner = True
    os.makedirs(mod.common.SCREEN_DIR)
    for name in ("output", "waybar.jsonc"):
        open(os.path.join(mod.common.SCREEN_DIR, name), "w").close()
    monkeypatch.setattr(mod.common, "close_all_menus", lambda: log.append("menus"))
    monkeypatch.setattr(mod.common, "drop_desktop_sound", lambda: log.append("sound"))

    def virtual_call(method, args, reply=None):
        log.append(method)
        raise GLib.Error("phoc is gone")
    session.virtual_call = virtual_call
    session.unnotify = lambda: log.append("unnotify")
    session.stop()
    assert session.stopping.is_set()
    assert log == [("terminate", "waybar"), ("terminate", "apps"), ("terminate", "events"),
                   ("kill", "waybar"), "menus", "sound", "Destroy", "unnotify"]
    # The screen's files go with it
    assert os.listdir(mod.common.SCREEN_DIR) == []


def test_stop_before_any_login(session, mod, monkeypatch):
    log = []
    session.wayvnc = Process(log, "wayvnc")
    monkeypatch.setattr(mod.common, "close_all_menus", lambda: log.append("menus"))
    monkeypatch.setattr(mod.common, "drop_desktop_sound", lambda: pytest.fail("no sound"))
    session.virtual_call = lambda *a: pytest.fail("no screen to destroy")
    session.unnotify = lambda: log.append("unnotify")
    session.stop()
    assert log == [("terminate", "wayvnc"), "menus", "unnotify"]


def test_notify_tells_the_address(session, mod):
    class Bus:
        def call_sync(self, name, path, iface, method, params, reply, *rest):
            self.params = params.unpack()
            return GLib.Variant("(u)", (42,))
    session.bus = bus = Bus()
    session.notify("10.0.0.5")
    assert session.notification == 42
    assert "10.0.0.5" in bus.params[4]
    # Not resident: a tap on it closes it
    assert "resident" not in bus.params[6]


# ----- wiring: buses, wayvnc and its events, the bar's edge -----

def test_init_connects_to_both_buses(mod, monkeypatch):
    asked = []
    monkeypatch.setattr(mod.Gio, "bus_get_sync",
                        lambda kind, cancel: asked.append(kind) or f"bus-{kind.value_nick}")
    s = mod.Session()
    assert asked == [mod.Gio.BusType.SESSION, mod.Gio.BusType.SYSTEM]
    assert (s.bus, s.system_bus) == ("bus-session", "bus-system")
    assert s.wayvnc is s.waybar is s.output is s.inhibitor is None
    assert s.notification == 0 and not s.stopping.is_set() and s.events.empty()


class Threads:
    """threading with Thread recorded instead of started."""

    def __init__(self):
        self.started = []

    def Thread(self, target, daemon):
        assert daemon
        return type("T", (), {"start": lambda _s: self.started.append(target.__name__)})()


def test_start_wayvnc_hands_over_the_socket(session, mod, fake_bin, monkeypatch):
    keys = fake_bin.stub("plinius-desktop-keys", "echo made the keys")
    monkeypatch.setattr(mod, "KEYS_TOOL", str(keys))
    monkeypatch.setattr(mod.common, "load", lambda: {"keyboard": "de-nodeadkeys"})
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setenv("LISTEN_PID", "123")
    monkeypatch.setenv("LISTEN_FDNAMES", "plinius-desktop.socket")
    started = []
    monkeypatch.setattr(mod, "subprocess", fakes.Proxy(
        subprocess, Popen=lambda cmd, **kw: started.append((cmd, kw)) or "wayvnc"))
    threads = Threads()
    monkeypatch.setattr(mod, "threading", threads)
    session.start_wayvnc()
    assert fake_bin.calls("plinius-desktop-keys") == [[]]
    (cmd, kw), = started
    assert cmd == ["wayvnc", "-C", os.path.join(mod.common.RUNTIME_DIR, "wayvnc.config"),
                   "-S", mod.common.CTL_SOCKET, "-n", GLib.get_host_name(),
                   "-k", "de-nodeadkeys", "-f", "60", "-x", "3"]
    assert kw["pass_fds"] == (3,)
    # wayvnc must not think the socket's variables are meant for it
    assert not {"LISTEN_FDS", "LISTEN_PID", "LISTEN_FDNAMES"} & set(kw["env"])
    assert session.wayvnc == "wayvnc"
    assert threads.started == ["watch_wayvnc", "read_events"]
    assert os.path.isfile(os.path.join(mod.common.RUNTIME_DIR, "wayvnc.config"))


def test_start_wayvnc_stops_when_the_keys_cannot_be_made(session, mod, fake_bin, monkeypatch):
    monkeypatch.setattr(mod, "KEYS_TOOL", str(fake_bin.stub("plinius-desktop-keys", "exit 1")))
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setattr(mod, "subprocess", fakes.Proxy(
        subprocess, Popen=lambda *a, **k: pytest.fail("no wayvnc without keys")))
    with pytest.raises(subprocess.CalledProcessError):
        session.start_wayvnc()
    assert session.wayvnc is None


def test_watch_wayvnc_reports_its_end(session):
    session.wayvnc = type("P", (), {"wait": lambda s: 0})()
    session.watch_wayvnc()
    assert session.events.get_nowait() == {"method": "wayvnc-exited"}


def test_read_events_queues_wayvncs_json_lines(session, mod, fake_bin, monkeypatch):
    fake_bin.stub("wayvncctl", """cat <<'EOF'
{"method": "client-connected", "params": {"address": "10.0.0.2"}}
not json
{"method": "client-disconnected", "params": {"connection_count": 0}}
EOF""")
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    session.read_events()
    assert fake_bin.calls("wayvncctl") == [["-S", mod.common.CTL_SOCKET, "-w", "-r", "-j",
                                            "event-receive"]]
    events = []
    while not session.events.empty():
        events.append(session.events.get_nowait())
    assert events == [{"method": "client-connected", "params": {"address": "10.0.0.2"}},
                      {"method": "client-disconnected", "params": {"connection_count": 0}}]
    assert session.events_process.returncode == 0


class PhocBus:
    def __init__(self):
        self.calls = []

    def call_sync(self, *args):
        self.calls.append(args)
        return GLib.Variant("(s)", ("HEADLESS-9",))


@pytest.mark.parametrize("reply", ["(s)", None])
def test_virtual_call_goes_to_phoc(session, mod, reply):
    session.bus = bus = PhocBus()
    args = GLib.Variant("(uudii)", (1920, 1080, 1.5, 1100, 0))
    session.virtual_call("Create", args, reply)
    (name, path, iface, method, params, reply_type, flags, timeout, cancel), = bus.calls
    assert (name, path, iface, method) == (
        "mobi.phosh.Phoc.VirtualOutput", "/mobi/phosh/Phoc/VirtualOutput",
        "mobi.phosh.Phoc.VirtualOutput", "Create")
    assert params is args and timeout == 10000
    assert (reply_type.dup_string() if reply_type else None) == reply


def test_screen_on_with_phone_sound_leaves_the_sound(session, mod, monkeypatch):
    session.virtual_call = lambda *a, **k: FakeVariantReply("HEADLESS-2")
    session.capture_screen = lambda: True
    session.start_bar = lambda: None
    monkeypatch.setattr(mod.common, "load", lambda: {"resolution": "1920x1080", "scale": "1",
                                                     "sound": "phone", "keyboard": "us"})
    monkeypatch.setattr(mod.common, "set_sound", lambda t: pytest.fail("sound stays"))
    threads = Threads()
    monkeypatch.setattr(mod, "threading", threads)
    session.screen_on()
    assert session.output == "HEADLESS-2"
    assert threads.started == ["watch_bar_position", "keep_layout", "keep_awake"]


class SchemaSource:
    def __init__(self, known):
        self.known = known

    def lookup(self, schema, recursive):
        return object() if self.known else None


def test_watch_bar_position_follows_gsettings(session, mod, fake_bin, monkeypatch):
    fake_bin.stub("gsettings", "echo \"bar-position: 'bottom'\"; echo \"bar-position: 'top'\"")
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(mod.Gio, SettingsSchemaSource=types.SimpleNamespace(
        get_default=lambda: SchemaSource(True))))
    session.watch_bar_position()
    assert fake_bin.calls("gsettings") == [["monitor", "org.plinius.desktop", "bar-position"]]
    assert [session.events.get_nowait() for _ in range(2)] == [{"method": "bar-moved"}] * 2
    assert session.events.empty() and session.monitor_process.returncode == 0


@pytest.mark.parametrize("source", [None, SchemaSource(False)])
def test_watch_bar_position_without_the_schema(session, mod, fake_bin, monkeypatch, source):
    fake_bin.stub("gsettings")
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(mod.Gio, SettingsSchemaSource=types.SimpleNamespace(
        get_default=lambda: source)))
    session.watch_bar_position()
    assert fake_bin.calls("gsettings") == [] and session.events.empty()


def test_keep_awake_survives_a_refused_inhibitor(session, capsys):
    class Bus(SystemBus):
        def call_with_unix_fd_list_sync(self, *args):
            self.inhibits += 1
            raise GLib.Error("access denied")
    session.system_bus = bus = Bus([False, False])
    session.stopping = Stopping([False, True])
    session.keep_awake()
    # Asked again at the next poll, never holding anything
    assert bus.inhibits == 2 and session.inhibitor is None
    assert capsys.readouterr().out.count("inhibitor: access denied") == 2


class NotifyBus:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def call_sync(self, name, path, iface, method, params, reply, flags, timeout, cancel):
        self.calls.append((method, params.unpack() if params else None))
        if self.fail:
            raise GLib.Error("no notification daemon")
        return GLib.Variant("(u)", (7,))


def test_notify_without_a_notification_daemon(session, capsys):
    session.bus = NotifyBus(fail=True)
    session.notify(None)
    assert session.notification == 0
    method, params = session.bus.calls[0]
    assert method == "Notify" and "A computer at ? is using this phone." == params[4]
    assert "notification: no notification daemon" in capsys.readouterr().out


@pytest.mark.parametrize("fail", [False, True])
def test_unnotify_closes_the_notification(session, fail):
    session.bus = NotifyBus(fail=fail)
    session.notification = 42
    session.unnotify()
    assert session.bus.calls == [("CloseNotification", (42,))]


def test_unnotify_without_a_notification(session):
    session.bus = NotifyBus()
    session.unnotify()
    assert session.bus.calls == []


def test_main_always_stops_the_session(mod, monkeypatch):
    log = []

    class Fake:
        def __init__(self, mode):
            log.append(mode)

        def run(self):
            log.append("run")
            raise RuntimeError("wayvnc broke")

        def stop(self):
            log.append("stop")
    monkeypatch.setattr(mod, "Session", Fake)
    with pytest.raises(RuntimeError):
        mod.main([])
    assert log == ["vnc", "run", "stop"]


def test_as_a_program_without_the_socket(mod, monkeypatch, tmp_path):
    import runpy
    import signal
    common = mod.common  # the desk_common in sys.modules, which the program imports
    assert __import__("sys").modules["desk_common"] is common
    handlers = []
    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.append(sig))
    monkeypatch.setattr(mod.Gio, "bus_get_sync", lambda kind, cancel: None)
    monkeypatch.delenv("LISTEN_FDS", raising=False)
    closed = []
    monkeypatch.setattr(common, "close_all_menus", lambda: closed.append(True))
    with pytest.raises(SystemExit) as exit_:
        runpy.run_path(str(DESK / "plinius-desktop-session"), run_name="__main__")
    assert "needs the socket of plinius-desktop.socket" in str(exit_.value.code)
    assert handlers == [signal.SIGTERM, signal.SIGINT]
    # The teardown ran: nothing was up, only the menus are closed
    assert closed == [True]


# ----- one virtual screen for VNC and the TV -----

def _create_fails(message):
    def virtual_call(method, args, reply=None):
        raise GLib.Error(message)
    return virtual_call


def test_acquire_screen_makes_and_owns_a_new_one(session, mod):
    calls = []
    session.virtual_call = lambda method, args, reply=None: (
        calls.append((method, args.unpack())) or FakeVariantReply("HEADLESS-3"))
    assert session.acquire_screen(1920, 1080, 1.125) == "HEADLESS-3"
    assert calls == [("Create", (1920, 1080, 1.125, 1100, 0))]
    assert session.owner
    assert os.stat(mod.common.SCREEN_DIR).st_mode & 0o777 == 0o700
    assert open(mod.common.OUTPUT_FILE).read() == "HEADLESS-3"


def test_acquire_screen_shares_the_one_of_the_other_session(session, mod):
    os.makedirs(mod.common.SCREEN_DIR)
    with open(mod.common.OUTPUT_FILE, "w") as f:
        f.write("HEADLESS-1\n")
    session.virtual_call = _create_fails("The virtual output HEADLESS-1 already exists")
    assert session.acquire_screen(1920, 1080, 1.0) == "HEADLESS-1"
    assert not session.owner
    # The owner's file stays as it is
    assert open(mod.common.OUTPUT_FILE).read() == "HEADLESS-1\n"


@pytest.mark.parametrize("message, name", [
    ("The virtual output HEADLESS-1 already exists", None),
    ("Invalid size, scale or position", "HEADLESS-1"),
])
def test_acquire_screen_without_a_screen_to_get(session, mod, message, name, capsys):
    if name:
        os.makedirs(mod.common.SCREEN_DIR)
        with open(mod.common.OUTPUT_FILE, "w") as f:
            f.write(name)
    session.virtual_call = _create_fails(message)
    assert session.acquire_screen(1920, 1080, 1.0) is None
    assert not session.owner
    assert "Create: " + message in capsys.readouterr().out


def test_screen_on_on_a_shared_screen_puts_nothing_on_it(session, mod, monkeypatch):
    def acquire(width, height, scale):
        assert (width, height, scale) == (1920, 1080, 1.0)
        return "HEADLESS-1"
    session.acquire_screen = acquire
    session.capture_screen = lambda: True
    session.start_bar = lambda: pytest.fail("the owner has the bar")
    monkeypatch.setattr(mod.common, "load", lambda: {"resolution": "1920x1080", "scale": "1",
                                                     "sound": "desktop", "keyboard": "us"})
    monkeypatch.setattr(mod.common, "set_sound", lambda t: pytest.fail("the TV keeps the sound"))
    threads = Threads()
    monkeypatch.setattr(mod, "threading", threads)
    session.screen_on()
    assert session.output == "HEADLESS-1"
    assert threads.started == ["watch_screen", "keep_awake"]


def test_screen_on_without_a_screen_stops(session, mod, monkeypatch):
    session.acquire_screen = lambda *a: None
    session.capture_screen = lambda: pytest.fail("nothing to capture")
    monkeypatch.setattr(mod.common, "load", lambda: {"resolution": "1920x1080", "scale": "1",
                                                     "sound": "phone", "keyboard": "us"})
    session.screen_on()
    assert session.events.get_nowait() == {"method": "stop"}


def test_watch_screen_ends_the_session_when_the_screen_is_gone(session, mod, monkeypatch):
    session.output = "HEADLESS-1"
    session.stopping = Stopping([False, False])
    layouts = [{"HEADLESS-1": {}, "DSI-1": {}}, {"DSI-1": {}}]
    monkeypatch.setattr(mod.common, "outputs", lambda: layouts.pop(0))
    session.watch_screen()
    assert session.events.get_nowait() == {"method": "screen-gone"}
    assert session.stopping.waits == [mod.LAYOUT_POLL, mod.LAYOUT_POLL]


def test_watch_screen_stops_with_the_session(session, mod, monkeypatch):
    session.output = "HEADLESS-1"
    monkeypatch.setattr(mod.common, "outputs", lambda: pytest.fail("stopped first"))
    session.watch_screen()
    assert session.events.empty()


@pytest.fixture
def cast(session, mod, monkeypatch):
    """run() in cast mode with the screen, the threads and systemd's socket faked."""
    session.mode = "cast"
    session.log = []
    monkeypatch.setattr(mod.signal, "signal", lambda *a: None)
    monkeypatch.setattr(mod.common, "load", lambda: {"resolution": "2560x1440", "scale": "1.5",
                                                     "sound": "desktop", "keyboard": "us"})
    session.threads = Threads()
    monkeypatch.setattr(mod, "threading", session.threads)
    monkeypatch.setattr(mod, "notify_ready", lambda: session.log.append("ready"))
    session.start_desktop = lambda: session.log.append("desktop")
    session.start_wayvnc = lambda: pytest.fail("no VNC server on a TV")
    return session


def test_run_cast_owns_a_full_hd_desktop(cast, mod):
    def acquire(width, height, scale):
        cast.log.append((width, height, scale))
        cast.owner = True
        return "HEADLESS-1"
    cast.acquire_screen = acquire
    put(cast, {"method": "bar-moved"}, {"method": "stop"})
    assert cast.run() == 0
    # The desktop as set up for VNC (2560x1440 at 1.5), only in the TV's size
    assert cast.log == [(1920, 1080, 1.125), "desktop", "ready"]
    assert cast.threads.started == ["keep_awake"]
    assert cast.output == "HEADLESS-1"


def test_run_cast_shares_the_vnc_desktop_until_it_is_gone(cast):
    cast.acquire_screen = lambda *a: "HEADLESS-1"
    put(cast, {"method": "screen-gone"})
    assert cast.run() == 0
    assert cast.log == ["ready"]
    assert cast.threads.started == ["watch_screen", "keep_awake"]


def test_run_cast_without_a_screen_fails_the_start(cast):
    cast.acquire_screen = lambda *a: None
    assert cast.run() == 1
    # systemd's start job fails, Schwupp shows no desktop
    assert cast.log == [] and cast.threads.started == []


def test_run_cast_with_settings_that_make_no_scale(cast, mod, monkeypatch):
    monkeypatch.setattr(mod.common, "load", lambda: {"resolution": "big", "scale": "1"})
    cast.acquire_screen = lambda w, h, scale: cast.log.append(scale)
    assert cast.run() == 1
    assert cast.log == [1.0]


def test_run_vnc_ends_when_the_shared_screen_is_gone(loop):
    put(loop, {"method": "client-connected", "params": {"address": "10.0.0.1"}},
        {"method": "screen-gone"}, {"method": "client-connected"})
    loop.run()
    assert loop.log == ["wayvnc", "screen", ("notify", "10.0.0.1")]


def test_stop_of_the_tv_owner_leaves_the_sound(session, mod, monkeypatch):
    log = []
    session.mode, session.owner, session.output = "cast", True, "HEADLESS-1"
    monkeypatch.setattr(mod.common, "close_all_menus", lambda: log.append("menus"))
    monkeypatch.setattr(mod.common, "drop_desktop_sound", lambda: pytest.fail("Schwupp's sound"))
    session.virtual_call = lambda method, args: log.append(method)
    session.unnotify = lambda: log.append("unnotify")
    # Nothing to remove is no failure
    session.stop()
    assert log == ["menus", "Destroy", "unnotify"]


def test_stop_of_a_shared_screen_leaves_it_to_its_owner(session, mod, monkeypatch):
    log = []
    session.output = "HEADLESS-1"
    os.makedirs(mod.common.SCREEN_DIR)
    open(mod.common.OUTPUT_FILE, "w").close()
    monkeypatch.setattr(mod.common, "close_all_menus", lambda: log.append("menus"))
    monkeypatch.setattr(mod.common, "drop_desktop_sound", lambda: pytest.fail("owner's sound"))
    session.virtual_call = lambda *a: pytest.fail("the owner destroys it")
    session.unnotify = lambda: log.append("unnotify")
    session.stop()
    assert log == ["menus", "unnotify"]
    assert os.path.exists(mod.common.OUTPUT_FILE)


# ----- systemd's start job -----

def test_notify_ready_without_systemd(mod, monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    mod.notify_ready()


def _receive(sock):
    sock.settimeout(5)
    return sock.recv(64)


def test_notify_ready_on_a_socket_path(mod, monkeypatch, tmp_path):
    import socket
    path = str(tmp_path / "notify")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind(path)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        mod.notify_ready()
        assert _receive(sock) == b"READY=1"


def test_notify_ready_on_an_abstract_socket(mod, monkeypatch):
    import socket
    name = f"plinius-desktop-test-{os.getpid()}"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind("\0" + name)
        monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)
        mod.notify_ready()
        assert _receive(sock) == b"READY=1"


@pytest.mark.parametrize("argv, mode, code, result", [
    (["--cast"], "cast", 1, 1),
    ([], "vnc", None, 0),
])
def test_main_picks_the_mode_and_passes_the_exit_code(mod, monkeypatch, argv, mode, code, result):
    log = []

    class Fake:
        def __init__(self, m):
            log.append(m)

        def run(self):
            return code

        def stop(self):
            log.append("stop")
    monkeypatch.setattr(mod, "Session", Fake)
    assert mod.main(argv) == result
    assert log == [mode, "stop"]


def test_main_reads_the_command_line(mod, monkeypatch):
    seen = []
    monkeypatch.setattr(mod, "Session", lambda m: seen.append(m) or types.SimpleNamespace(
        run=lambda: 0, stop=lambda: None))
    monkeypatch.setattr(mod.sys, "argv", ["plinius-desktop-session", "--cast"])
    assert mod.main() == 0
    assert seen == ["cast"]
