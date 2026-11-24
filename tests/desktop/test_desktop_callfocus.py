# plinius-callfocus: which calls and recordings take the sound, which players and streams make
# way, and that exactly those come back. The session bus, pactl and the main loop are faked; the
# state file lives in tmp_path.

import json
import os
import types

import pytest
from gi.repository import Gio, GLib

import desktop_fakes as fakes
from conftest import PKG, load_script
from desktop_mainrun import run_as_main

CALLFOCUS = PKG / "plinius-callfocus" / "plinius-callfocus.py"


@pytest.fixture(scope="module")
def mod():
    return load_script(CALLFOCUS, "plinius_callfocus_under_test")


class Players:
    """MPRIS players as the session bus would show them."""

    def __init__(self, **status):
        self.status = dict(status)
        self.calls = []
        self.fail = set()

    def names(self):
        return ["org.mpris.MediaPlayer2." + n for n in self.status]

    def call(self, name, method):
        short = name.rsplit(".", 1)[1]
        self.calls.append((short, method))
        if short in self.fail:
            return False
        self.status[short] = {"Pause": "Paused", "Play": "Playing"}[method]
        return True


@pytest.fixture
def focus(mod, tmp_path, monkeypatch):
    glib = fakes.FakeGLib()
    monkeypatch.setattr(mod, "GLib", glib)
    monkeypatch.setattr(mod, "STATE_FILE", str(tmp_path / "plinius-callfocus.json"))
    f = mod.CallFocus.__new__(mod.CallFocus)
    f.calls, f.paused, f.muted = {}, [], []
    f.focused = f.recording = f.recorded = f.call_muted = False
    f.resume_source = f.settle_source = 0
    f.last_pause = {}
    f.glib = glib
    f.players_ = players = Players()
    f.players = players.names
    f.player_status = lambda name: players.status.get(name.rsplit(".", 1)[1])
    f.player_call = players.call
    f.owner = lambda name: ":1." + name.rsplit(".", 1)[1]
    f.streams = []
    f.mutes = []
    f.playing_streams = lambda: list(f.streams)

    def set_mute(index, mute):
        f.mutes.append((index, mute))
        return index != 99  # 99: a stream that ended meanwhile
    f.set_mute = set_mute
    return f


def resume(focus):
    (sid,) = focus.glib.of_kind("timeout")
    delay = focus.glib.sources[sid][1]
    focus.glib.fire(sid)
    return delay


@pytest.mark.parametrize("state, counts", [(0, False), (1, True), (2, True), (3, True),
                                           (4, True), (5, True), (6, True), (7, False)])
def test_in_call_state(mod, state, counts):
    assert mod.in_call_state(state) is counts


def test_a_call_pauses_players_and_mutes_streams(mod, focus):
    focus.players_.status = {"emilia": "Playing", "firefox": "Paused", "mpv": "Playing"}
    focus.streams = [(4, "Radio"), (99, "Gone")]
    focus.calls["/org/gnome/Calls/Call/1"] = 5  # incoming
    focus.update()
    assert focus.focused
    assert focus.paused == ["org.mpris.MediaPlayer2.emilia", "org.mpris.MediaPlayer2.mpv"]
    assert focus.muted == [(4, "Radio")]
    with open(mod.STATE_FILE) as f:
        assert json.load(f) == ["Radio"]
    # Answered, then hung up: one give-back after the bridge left call mode
    focus.calls["/org/gnome/Calls/Call/1"] = 1
    focus.update()
    focus.calls["/org/gnome/Calls/Call/1"] = 7
    focus.update()
    assert resume(focus) == mod.RESUME_DELAY_MS
    assert focus.mutes == [(4, True), (99, True), (4, False)]
    assert focus.players_.status == {"emilia": "Playing", "firefox": "Paused", "mpv": "Playing"}
    assert focus.paused == [] and focus.muted == []
    assert not os.path.exists(mod.STATE_FILE)


def test_a_player_the_user_touched_is_left_alone(focus):
    focus.players_.status = {"emilia": "Playing", "mpv": "Playing"}
    focus.calls["/c/1"] = 1
    focus.update()
    focus.players_.status["mpv"] = "Stopped"  # the user stopped it during the call
    focus.calls.clear()
    focus.update()
    resume(focus)
    assert ("mpv", "Play") not in focus.players_.calls
    assert focus.players_.status["emilia"] == "Playing"


def test_a_failed_pause_is_not_resumed(focus):
    focus.players_.status = {"emilia": "Playing"}
    focus.players_.fail = {"emilia"}
    focus.calls["/c/1"] = 3
    focus.update()
    assert focus.paused == []


def test_two_calls_are_one_focus(focus):
    focus.players_.status = {"emilia": "Playing"}
    focus.calls["/c/1"] = 1
    focus.update()
    focus.calls["/c/2"] = 6  # waiting
    focus.update()
    focus.calls.pop("/c/1")
    focus.update()
    assert focus.glib.sources == {}
    focus.calls.pop("/c/2")
    focus.update()
    resume(focus)
    assert focus.players_.calls == [("emilia", "Pause"), ("emilia", "Play")]


def test_a_call_right_after_the_last_keeps_the_focus(focus):
    focus.players_.status = {"emilia": "Playing"}
    focus.calls["/c/1"] = 1
    focus.update()
    focus.calls["/c/1"] = 7
    focus.update()
    (sid,) = focus.glib.of_kind("timeout")
    focus.calls["/c/2"] = 5
    focus.update()
    assert sid in focus.glib.removed and focus.glib.sources == {}
    assert focus.players_.calls == [("emilia", "Pause")]
    focus.calls["/c/2"] = 7
    focus.update()
    resume(focus)
    assert focus.players_.calls == [("emilia", "Pause"), ("emilia", "Play")]


def test_a_recording_pauses_players_but_mutes_nothing(mod, focus):
    focus.players_.status = {"emilia": "Playing"}
    focus.streams = [(4, "Radio")]
    focus.recording = True
    focus.update()
    assert focus.paused == ["org.mpris.MediaPlayer2.emilia"] and focus.muted == []
    focus.recording = False
    focus.update()
    # The headset needs longer to come back from hands-free
    assert resume(focus) == mod.RECORDING_RESUME_DELAY_MS
    assert focus.players_.status["emilia"] == "Playing" and focus.recorded is False


def test_a_player_that_paused_itself_just_before_a_recording(mod, focus, monkeypatch):
    now = [500.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    focus.players_.status = {"emilia": "Paused", "firefox": "Paused"}
    # The headset's AVRCP pause, seen 1 s before; Firefox was paused long ago
    focus.on_player_changed(None, ":1.emilia", None, None, None,
                            GLib.Variant("(sa{sv}as)", (
                                "org.mpris.MediaPlayer2.Player",
                                {"PlaybackStatus": GLib.Variant("s", "Paused")}, [])))
    focus.last_pause[":1.firefox"] = now[0] - 60
    now[0] += 1
    focus.recording = True
    focus.update()
    assert focus.paused == ["org.mpris.MediaPlayer2.emilia"]


def test_self_paused_players_do_not_count_for_a_call(focus):
    focus.players_.status = {"emilia": "Paused"}
    focus.last_pause[":1.emilia"] = 10 ** 9
    focus.calls["/c/1"] = 5
    focus.update()
    assert focus.paused == []


def test_playing_again_forgets_the_pause(focus):
    focus.last_pause[":1.x"] = 1.0
    for status, kept in (("Playing", False), (None, False)):
        props = {"PlaybackStatus": GLib.Variant("s", status)} if status else {}
        focus.on_player_changed(None, ":1.x", None, None, None, GLib.Variant(
            "(sa{sv}as)", ("org.mpris.MediaPlayer2.Player", props, [])))
        assert (":1.x" in focus.last_pause) is kept


def test_a_call_during_the_give_back_of_a_recording_mutes_streams(focus):
    focus.streams = [(4, "Radio")]
    focus.recording = True
    focus.update()
    focus.recording = False
    focus.update()
    focus.calls["/c/1"] = 5
    focus.update()
    assert (4, True) in focus.mutes


# ----- the calls from GNOME Calls -----

class Proxy:
    def __init__(self, state, iface="org.gnome.Calls.Call"):
        self.state, self.iface = state, iface

    def get_cached_property(self, name):
        return None if self.state is None else GLib.Variant("i", self.state)

    def get_interface_name(self):
        return self.iface


class Obj:
    def __init__(self, path, proxy):
        self.path, self.proxy = path, proxy

    def get_object_path(self):
        return self.path

    def get_interface(self, iface):
        return self.proxy if self.proxy and iface == self.proxy.iface else None


def test_tracking_calls(focus):
    updates = []
    focus.update = lambda: updates.append(dict(focus.calls))
    focus.track(Obj("/c/1", Proxy(5)))
    focus.track(Obj("/c/x", None))  # not a call
    focus.track(Obj("/c/2", Proxy(None)))
    assert focus.calls == {"/c/1": 5, "/c/2": 0}
    focus.on_properties(None, Obj("/c/1", None), Proxy(None), {"State": 1}, [])
    focus.on_properties(None, Obj("/c/1", None), Proxy(None, "other"), {"State": 7}, [])
    focus.on_properties(None, Obj("/c/1", None), Proxy(None), {"Name": "x"}, [])
    assert focus.calls["/c/1"] == 1
    focus.forget("/c/1")
    focus.forget("/c/9")
    assert focus.calls == {"/c/2": 0} and len(updates) == 4
    focus.on_calls_vanished(None, None)
    assert focus.calls == {} and focus.manager is None


# ----- recordings and streams from pactl -----

SOURCE_OUTPUTS = [
    {"index": 1, "corked": False, "properties": {"application.name": "Stevia",
                                                  "application.process.binary": "stevia"}},
    {"index": 2, "corked": True, "properties": {"application.name": "Paused rec"}},
    {"index": 3, "properties": {"application.process.binary": "audio-hal-bridge"}},
    {"index": 4, "properties": {"node.link-group": "loopback-1"}},
    {"index": 5, "properties": {"stream.monitor": "true"}},
    {"index": 6, "properties": {"media.name": "Peak detect"}},
    {"index": 7, "properties": {"node.virtual": "true"}},
    {"index": 8, "properties": {"stream.capture.sink": "true"}},
    {"index": 9, "properties": {"application.process.binary": "arecord"}},
    {"index": 10, "properties": {}},
]


def test_recording_apps(mod, focus, monkeypatch):
    monkeypatch.setattr(mod.CallFocus, "source_outputs", staticmethod(lambda: SOURCE_OUTPUTS))
    assert focus.recording_apps() == ["Stevia", "arecord", "10"]


def test_check_recording_updates_on_a_change_only(mod, focus, monkeypatch):
    outputs = [[], SOURCE_OUTPUTS[:1], SOURCE_OUTPUTS[:1], []]
    monkeypatch.setattr(mod.CallFocus, "source_outputs", staticmethod(lambda: outputs.pop(0)))
    updates = []
    focus.update = lambda: updates.append(focus.recording)
    for _ in range(4):
        focus.check_recording()
    assert updates == [True, False]


SINK_INPUTS = [
    {"index": 1, "mute": False, "corked": False, "properties": {"application.name": "Radio"}},
    {"index": 2, "mute": True, "properties": {"application.name": "Muted"}},
    {"index": 3, "corked": True, "properties": {"application.name": "Corked"}},
    {"index": 4, "properties": {"media.role": "Phone"}},
    {"index": 5, "properties": {"media.role": "notification"}},
    {"index": 6, "properties": {"application.process.binary": "feedbackd"}},
    {"index": 7, "properties": {"node.link-group": "x"}},
    {"index": 8, "properties": {"node.virtual": "true"}},
    {"index": 9, "properties": {"application.process.binary": "mpg123", "media.role": "music"}},
]


def test_playing_streams(mod, focus, monkeypatch):
    monkeypatch.setattr(mod.CallFocus, "sink_inputs", staticmethod(lambda: SINK_INPUTS))
    assert mod.CallFocus.playing_streams(focus) == [(1, "Radio"), (9, "mpg123")]


@pytest.fixture
def pactl(mod, fake_bin, monkeypatch):
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    return fake_bin


def test_pactl_json_lists(mod, pactl):
    pactl.stub("pactl", """[ "$4" = sink-inputs ] && echo '[{"index": 3}]'
[ "$4" = source-outputs ] && echo 'not json'
exit 0""")
    assert mod.CallFocus.sink_inputs() == [{"index": 3}]
    assert mod.CallFocus.source_outputs() == []
    assert pactl.calls("pactl") == [["-f", "json", "list", "sink-inputs"],
                                    ["-f", "json", "list", "source-outputs"]]


def test_pactl_failing(mod, pactl):
    pactl.stub("pactl", "exit 1")
    assert mod.CallFocus.sink_inputs() == []
    assert mod.CallFocus.set_mute(4, True) is False
    pactl.stub("pactl", "exit 0")
    assert mod.CallFocus.set_mute(4, False) is True
    assert pactl.calls("pactl")[-1] == ["set-sink-input-mute", "4", "0"]


def test_unmute_leftovers_of_an_earlier_run(mod, pactl, tmp_path, monkeypatch):
    state = tmp_path / "plinius-callfocus.json"
    monkeypatch.setattr(mod, "STATE_FILE", str(state))
    state.write_text(json.dumps(["Radio", "mpg123"]))
    pactl.stub("pactl", """[ "$4" = sink-inputs ] && cat <<'EOF'
[{"index": 11, "mute": true, "properties": {"application.name": "Radio"}},
 {"index": 12, "mute": true, "properties": {"application.process.binary": "mpg123"}},
 {"index": 13, "mute": true, "properties": {"application.name": "Muted by the user"}},
 {"index": 14, "mute": false, "properties": {"application.name": "Radio"}}]
EOF
exit 0""")
    f = mod.CallFocus.__new__(mod.CallFocus)
    f.unmute_leftovers()
    mutes = [c for c in pactl.calls("pactl") if c[0] == "set-sink-input-mute"]
    assert mutes == [["set-sink-input-mute", "11", "0"], ["set-sink-input-mute", "12", "0"]]
    assert not state.exists()


def test_unmute_leftovers_without_a_file(mod, pactl, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "STATE_FILE", str(tmp_path / "none.json"))
    pactl.stub("pactl", "exit 0")
    mod.CallFocus.__new__(mod.CallFocus).unmute_leftovers()
    assert pactl.calls("pactl") == []


# ----- following pactl subscribe -----

class Lines:
    def __init__(self, line):
        self.line = line
        self.reads = 0

    def read_line_finish_utf8(self, result):
        if isinstance(self.line, Exception):
            raise self.line
        return self.line, len(self.line or "")

    def read_line_async(self, *args):
        self.reads += 1


class Proc:
    exited = False

    def force_exit(self):
        self.exited = True


def test_source_output_events_settle_first(mod, focus):
    checks = []
    focus.check_recording = lambda: checks.append(1)
    for line in ("Event 'new' on source-output #5", "Event 'change' on source-output #5",
                 "Event 'change' on sink #1"):
        lines = Lines(line)
        focus.on_event(lines, None, Proc())
        assert lines.reads == 1
    (sid,) = focus.glib.of_kind("timeout")
    assert focus.glib.sources[sid][1] == mod.RECORDING_SETTLE_MS
    focus.glib.fire(sid)
    assert checks == [1] and focus.settle_source == 0


@pytest.mark.parametrize("line", [None, fakes.gerror("closed")])
def test_pactl_ending_is_followed_again(focus, line):
    proc = Proc()
    lines = Lines(line)
    focus.on_event(lines, None, proc)
    assert proc.exited and lines.reads == 0
    (sid,) = focus.glib.of_kind("seconds")
    assert focus.glib.sources[sid][1] == 2


def test_a_recording_right_after_a_call_keeps_the_focus_without_muting(focus):
    focus.players_.status = {"emilia": "Playing"}
    focus.streams = [(4, "Radio")]
    focus.calls["/c/1"] = 1
    focus.update()
    focus.calls["/c/1"] = 7
    focus.update()
    (sid,) = focus.glib.of_kind("timeout")
    focus.mutes.clear()
    focus.recording = True  # dictation right after hanging up
    focus.update()
    assert sid in focus.glib.removed and focus.focused
    assert focus.mutes == [] and focus.paused == ["org.mpris.MediaPlayer2.emilia"]


def test_hidden_sources_unreadable_counts_every_recording(mod, pactl):
    pactl.stub("pactl", """[ "$4" = source-outputs ] && echo '[{"index": 1, "source": 7,
  "properties": {"application.name": "Radio"}}]' && exit 0
exit 1""")
    f = mod.CallFocus.__new__(mod.CallFocus)
    assert mod.CallFocus.hidden_sources() == set()
    assert f.recording_apps() == ["Radio"]


def test_hidden_sources_leave_out_the_tuner(mod, pactl):
    pactl.stub("pactl", """[ "$4" = source-outputs ] && echo '[{"index": 1, "source": 7,
  "properties": {"application.name": "Radio"}}]'
[ "$4" = sources ] && echo '[{"index": 7, "properties": {"plinius.hidden": "true"}},
  {"index": 8, "properties": {}}]'
exit 0""")
    f = mod.CallFocus.__new__(mod.CallFocus)
    assert mod.CallFocus.hidden_sources() == {7}
    assert f.recording_apps() == []


def test_save_state_failing_is_logged(mod, focus, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "STATE_FILE", str(tmp_path / "missing-dir" / "state.json"))
    focus.muted = [(4, "Radio")]
    focus.save_state()
    assert "cannot keep the muted apps:" in capsys.readouterr().err


# ----- MPRIS players over the session bus -----

class SessionBus:
    """The session bus as far as callfocus uses it: the bus daemon and MPRIS players."""

    def __init__(self, players=None, fail=()):
        self.players = dict(players or {})   # short name -> (owner, status)
        self.fail = set(fail)                 # short names whose calls fail
        self.calls = []
        self.subscriptions = []
        self.list_fails = False

    def call_sync(self, name, path, iface, method, params, reply, flags, timeout, cancel):
        self.calls.append((name, method, params.unpack() if params is not None else None))
        if name == "org.freedesktop.DBus":
            if method == "ListNames":
                if self.list_fails:
                    raise GLib.Error("no bus")
                return GLib.Variant("(as)", ([":1.1", "org.gnome.Calls",
                                              "org.mpris.MediaPlayer2.playerctld"]
                                             + ["org.mpris.MediaPlayer2." + n
                                                for n in self.players],))
            short = params.unpack()[0].rsplit(".", 1)[1]
            if short not in self.players:
                raise GLib.Error("no owner")
            return GLib.Variant("(s)", (self.players[short][0],))
        short = name.rsplit(".", 1)[1]
        if short in self.fail or short not in self.players:
            raise GLib.Error(f"{short} gone")
        owner, status = self.players[short]
        if method == "Get":
            return GLib.Variant("(v)", (GLib.Variant("s", status),))
        self.players[short] = (owner, {"Pause": "Paused", "Play": "Playing"}[method])
        return None

    def signal_subscribe(self, *args):
        self.subscriptions.append(args)
        return len(self.subscriptions)


def test_players_owner_status_and_calls_over_the_bus(mod, capsys):
    f = mod.CallFocus.__new__(mod.CallFocus)
    f.bus = SessionBus({"emilia": (":1.20", "Playing"), "mpv": (":1.21", "Paused")},
                       fail={"mpv"})
    # playerctld only passes on, and other names are no players
    assert f.players() == ["org.mpris.MediaPlayer2.emilia", "org.mpris.MediaPlayer2.mpv"]
    assert f.owner("org.mpris.MediaPlayer2.emilia") == ":1.20"
    assert f.owner("org.mpris.MediaPlayer2.gone") is None
    assert f.player_status("org.mpris.MediaPlayer2.emilia") == "Playing"
    assert f.player_status("org.mpris.MediaPlayer2.mpv") is None
    assert f.player_call("org.mpris.MediaPlayer2.emilia", "Pause") is True
    assert f.bus.players["emilia"] == (":1.20", "Paused")
    assert f.player_call("org.mpris.MediaPlayer2.mpv", "Play") is False
    assert "Play on org.mpris.MediaPlayer2.mpv failed: mpv gone" in capsys.readouterr().err
    assert ("org.mpris.MediaPlayer2.emilia", "Get",
            ("org.mpris.MediaPlayer2.Player", "PlaybackStatus")) in f.bus.calls
    f.bus.list_fails = True
    assert f.players() == []


# ----- the program as the session starts it -----

class Manager:
    """Gio.DBusObjectManagerClient of GNOME Calls."""

    def __init__(self, objects):
        self.objects = objects
        self.handlers = {}

    def connect(self, signal, handler):
        self.handlers[signal] = handler

    def get_objects(self):
        return list(self.objects)


class Stream:
    """The stdout of `pactl subscribe` as a Gio.DataInputStream."""

    def __init__(self):
        self.reads = []

    def read_line_async(self, priority, cancellable, callback, proc):
        self.reads.append((callback, proc))


def callfocus_gio(bus, manager, watches, streams, subprocess_errors=()):
    errors = list(subprocess_errors)

    def new_sync(connection, flags, name, path, *rest):
        assert (connection, name, path) == (bus, "org.gnome.Calls", "/org/gnome/Calls")
        if isinstance(manager, Exception):
            raise manager
        return manager

    def subprocess_new(argv, flags):
        assert argv == ["pactl", "subscribe"]
        if errors:
            raise errors.pop(0)
        return types.SimpleNamespace(get_stdout_pipe=lambda: "pipe")

    def stream_new(pipe):
        streams.append(Stream())
        return streams[-1]

    return fakes.Proxy(
        Gio, bus_get_sync=lambda kind, cancellable: bus,
        bus_watch_name_on_connection=lambda conn, name, flags, up, down: watches.append(
            (name, up, down)),
        DBusObjectManagerClient=types.SimpleNamespace(new_sync=new_sync),
        Subprocess=types.SimpleNamespace(new=subprocess_new),
        DataInputStream=types.SimpleNamespace(new=stream_new))


def test_program_pauses_and_mutes_for_a_call_and_gives_back(mod, pactl, tmp_path, capsys):
    pactl.stub("pactl", """[ "$4" = sink-inputs ] && echo '[{"index": 4, "mute": false,
  "properties": {"application.name": "Radio"}}]'
[ "$4" = source-outputs ] && echo '[]'
exit 0""")
    runtime = tmp_path / "run"
    runtime.mkdir()
    # An earlier run died during a call and left the radio muted
    (runtime / "plinius-callfocus.json").write_text('["Gone app"]')
    bus = SessionBus({"emilia": (":1.20", "Playing"), "firefox": (":1.21", "Paused")})
    manager = Manager([Obj("/org/gnome/Calls/Call/1", Proxy(5))])
    watches, streams, seen = [], [], {}

    def during_run(glib):
        assert not (runtime / "plinius-callfocus.json").exists()  # leftovers handled
        assert [s[2] for s in bus.subscriptions] == ["PropertiesChanged"]
        (name, up, down), = watches
        assert name == "org.gnome.Calls"
        assert len(streams) == 1 and len(streams[0].reads) == 1  # following pactl
        up(bus, name, ":1.9")
        # The incoming call: emilia paused, the radio muted and written down
        assert bus.players["emilia"] == (":1.20", "Paused")
        assert json.loads((runtime / "plinius-callfocus.json").read_text()) == ["Radio"]
        # Hung up: given back after the bridge's delay
        manager.handlers["interface-proxy-properties-changed"](
            manager, Obj("/org/gnome/Calls/Call/1", None), Proxy(None), {"State": 7}, [])
        (sid,) = glib.of_kind("timeout")
        assert glib.sources[sid][1] == mod.RESUME_DELAY_MS
        glib.fire(sid)
        seen["after"] = (bus.players["firefox"], bus.players["emilia"])
        # A second call object comes and goes
        manager.handlers["object-added"](manager, Obj("/org/gnome/Calls/Call/2", Proxy(7)))
        manager.handlers["object-removed"](manager, Obj("/org/gnome/Calls/Call/2", None))
        down(bus, name)

    glib = fakes.FakeGLib(get_user_runtime_dir=lambda: str(runtime))
    glib.MainLoop = lambda: types.SimpleNamespace(run=lambda: during_run(glib))
    gio = callfocus_gio(bus, manager, watches, streams)
    assert run_as_main(CALLFOCUS, Gio=gio, GLib=glib) is None
    assert seen["after"] == ((":1.21", "Paused"), (":1.20", "Playing"))
    mutes = [c for c in pactl.calls("pactl") if c[0] == "set-sink-input-mute"]
    assert mutes == [["set-sink-input-mute", "4", "1"], ["set-sink-input-mute", "4", "0"]]
    assert not (runtime / "plinius-callfocus.json").exists()
    assert "unmuted after an earlier run: ['Gone app']" in capsys.readouterr().err


def test_calls_without_an_object_manager(mod, monkeypatch, capsys):
    bus = SessionBus()
    f = mod.CallFocus.__new__(mod.CallFocus)
    f.bus, f.manager = bus, None
    monkeypatch.setattr(mod, "Gio", callfocus_gio(bus, GLib.Error("Calls is gone"), [], []))
    f.on_calls_appeared(bus, "org.gnome.Calls", ":1.9")
    assert f.manager is None
    assert "no object manager of Calls: Calls is gone" in capsys.readouterr().err


def test_pactl_not_startable_is_tried_again(mod, focus, monkeypatch, capsys):
    streams = []
    monkeypatch.setattr(mod, "Gio", callfocus_gio(None, None, [], streams,
                                                  [GLib.Error("no pactl")]))
    checks = []
    focus.check_recording = lambda: checks.append(1)
    mod.CallFocus.watch_recordings(focus)
    assert "cannot follow the recordings: no pactl" in capsys.readouterr().err
    (sid,) = focus.glib.of_kind("seconds")
    assert focus.glib.sources[sid][1] == 10 and streams == []
    assert focus.glib.fire(sid) is False  # one retry source, which starts it
    assert len(streams) == 1 and len(streams[0].reads) == 1 and checks == [1]
    assert focus.glib.sources == {}


def test_a_call_during_a_recording_mutes_streams(mod, focus, tmp_path, capsys):
    focus.streams = [(4, "Radio")]
    focus.recording = True
    focus.update()
    assert focus.mutes == []  # a recording mutes nothing
    focus.calls["/c/1"] = 5   # a call rings while the app still records
    focus.update()
    assert focus.mutes == [(4, True)] and focus.muted == [(4, "Radio")]
    assert json.loads((tmp_path / "plinius-callfocus.json").read_text()) == ["Radio"]
    assert "call: muted ['Radio']" in capsys.readouterr().err
    # Answered: muted once per focus, like a call that took the focus itself
    focus.calls["/c/1"] = 1
    focus.update()
    assert focus.mutes == [(4, True)]
    # The call and then the recording end: the focus is given back once, everything unmuted
    del focus.calls["/c/1"]
    focus.update()
    assert focus.glib.of_kind("timeout") == []
    focus.recording = False
    focus.update()
    assert resume(focus) == mod.RECORDING_RESUME_DELAY_MS
    assert focus.mutes[1:] == [(4, False)] and focus.muted == [] and not focus.call_muted
    assert not (tmp_path / "plinius-callfocus.json").exists()


def test_a_call_during_a_recording_with_nothing_playing(mod, focus, tmp_path, capsys):
    focus.recording = True
    focus.update()
    focus.calls["/c/1"] = 5
    focus.update()
    assert focus.mutes == [] and focus.call_muted
    assert not (tmp_path / "plinius-callfocus.json").exists()
    assert "call: muted" not in capsys.readouterr().err
    focus.streams = [(4, "Radio")]  # started during the call: left alone, as in a call alone
    focus.calls["/c/1"] = 1
    focus.update()
    assert focus.mutes == []
