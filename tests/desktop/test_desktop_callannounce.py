# plinius-callannounce: what is said for a call, which Bluetooth devices count as a headset (never
# a car kit or a loudspeaker), when the announcement starts (after the first ring, only while the
# ringtone sounds and a headset in A2DP is the output), that the ringtone is muted meanwhile, and
# that answering, declining or switching it off stops it at once. Buses, pactl and the main loop
# are faked.

import json
import subprocess
import types

import pytest
from gi.repository import Gio, GLib

import desktop_fakes as fakes
from conftest import PKG, load_script

ANNOUNCE = PKG / "plinius-speech" / "plinius-callannounce"

HEADSET = 0x240404   # Audio/Video, wearable headset
PBAP_PCE = "0000112e-0000-1000-8000-00805f9b34fb"
MAP_MCE = "00001133-0000-1000-8000-00805f9b34fb"
HFP = "0000111e-0000-1000-8000-00805f9b34fb"


@pytest.fixture(scope="module")
def mod():
    return load_script(ANNOUNCE, "plinius_callannounce_under_test")


@pytest.mark.parametrize("name,number,text", [
    ("Daniel Müller", "+4915112345678", "Daniel Müller is calling"),
    ("  Daniel  ", "", "Daniel is calling"),
    ("", "+4915112345678", "Unknown caller"),            # no contact
    ("+49 151 1234-5678", "+4915112345678", "Unknown caller"),  # only the number
    ("0151", "0151", "Unknown caller"),
    (None, None, "Unknown caller"),                      # withheld
    ("112", "", "Unknown caller"),
])
def test_caller_text(mod, name, number, text):
    assert mod.caller_text(name, number) == text


@pytest.mark.parametrize("cls,uuids,icon,headset", [
    (0x240404, [HFP], "audio-headset", True),           # wearable headset
    (0x240408, [HFP], "", True),                        # hands-free
    (0x240418, [], "", True),                           # headphones
    (0x240420, [HFP], "audio-card", False),             # car audio
    (0x240408, [HFP, PBAP_PCE.upper()], "", False),     # hands-free that reads the phone book: a car
    (0x240404, [MAP_MCE], "", False),                   # reads messages: a car
    (0x240414, [], "", False),                          # loudspeaker
    (0x5a020c, [], "phone", False),                     # a phone
    (0, [], "audio-headphones", True),                  # LE Audio without a class: the icon
    (0, None, "audio-card", False),
])
def test_is_headset(mod, cls, uuids, icon, headset):
    assert mod.is_headset(cls, uuids, icon) is headset


class Run:
    """subprocess.run for pactl: answers per argument list."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv[1:])
        a = self.answers.get(tuple(argv[1:]))
        if isinstance(a, Exception):
            raise a
        return types.SimpleNamespace(stdout=a if isinstance(a, str) else json.dumps(a), returncode=0)


SINKS = [{"name": "alsa_output.x", "properties": {}},
         {"name": "bluez_output.AA_BB.1", "properties": {"api.bluez5.profile": "a2dp-sink",
                                                       "api.bluez5.address": "aa:bb"}}]
INPUTS = [{"index": 7, "properties": {"application.process.binary": "feedbackd", "media.role": "Ringtone"}},
          {"index": 8, "properties": {"application.process.binary": "feedbackd", "media.role": "Event"}},
          {"index": 9, "properties": {"application.process.binary": "amberol", "media.role": "Ringtone"}},
          {"properties": {"application.process.binary": "feedbackd", "media.role": "ringtone"}}]


def pactl(mod, monkeypatch, default="bluez_output.AA_BB.1\n", sinks=SINKS, inputs=INPUTS, more=None):
    answers = {("get-default-sink",): default, ("-f", "json", "list", "sinks"): sinks,
               ("-f", "json", "list", "sink-inputs"): inputs}
    answers.update(more or {})
    run = Run(answers)
    monkeypatch.setattr(mod.subprocess, "run", run)
    return run


def test_headset_address(mod, monkeypatch):
    pactl(mod, monkeypatch)
    assert mod.headset_address() == "aa:bb"
    pactl(mod, monkeypatch, default="hal_output\n")
    assert mod.headset_address() is None
    # In hands-free (a call already runs), or the sink is not listed
    hfp = [{"name": "bluez_output.AA_BB.1", "properties": {"api.bluez5.profile": "headset-head-unit"}}]
    pactl(mod, monkeypatch, sinks=hfp)
    assert mod.headset_address() is None
    pactl(mod, monkeypatch, sinks=[])
    assert mod.headset_address() is None
    pactl(mod, monkeypatch, default=subprocess.TimeoutExpired("pactl", 5))
    assert mod.headset_address() is None


def test_pactl_failures(mod, monkeypatch, capsys):
    pactl(mod, monkeypatch, inputs="not json")
    assert mod.ringtone_inputs() == []
    pactl(mod, monkeypatch, more={("-f", "json", "list", "sink-inputs"): OSError("no pactl")})
    assert mod.ringtone_inputs() == []
    pactl(mod, monkeypatch, more={("set-sink-input-mute", "7", "1"): OSError("gone")})
    assert mod.pactl("set-sink-input-mute", "7", "1") is False
    assert "failed" in capsys.readouterr().err


def test_ringtone_inputs(mod, monkeypatch):
    pactl(mod, monkeypatch)
    assert mod.ringtone_inputs() == [7]       # feedbackd's ringtone only, with an index


class Bus:
    """The session bus: Speak/Stop/Prepare of plinius-speechd, signal subscriptions."""

    def __init__(self):
        self.calls = []
        self.subs = []
        self.next_id = 41
        self.fail = set()

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, cb):
        self.subs.append((member, cb))
        return 1

    def call(self, dest, path, iface, method, params, *rest):
        self.calls.append((method, params.unpack() if params else None, "async"))

    def call_sync(self, dest, path, iface, method, params, *rest):
        self.calls.append((method, params.unpack() if params else None))
        if method in self.fail:
            raise GLib.Error("speech failed")
        if method == "Speak":
            self.next_id += 1
            return GLib.Variant("(u)", (self.next_id,))
        return None

    def finished(self, sid, completed=True):
        for member, cb in self.subs:
            if member == "Finished":
                cb(self, ":1.5", "/org/plinius/Speech1", "org.plinius.Speech1", "Finished",
                   GLib.Variant("(ub)", (sid, completed)))

    def methods(self):
        return [c[0] for c in self.calls]


class SystemBus:
    def __init__(self, devices):
        self.devices = devices
        self.fail = False

    def call_sync(self, dest, path, iface, method, params, reply_type, *rest):
        if self.fail:
            raise GLib.Error("no bluez")
        objs = {f"/org/bluez/hci0/dev_{i}": {"org.bluez.Device1": d} for i, d in enumerate(self.devices)}
        objs["/org/bluez/hci0"] = {"org.bluez.Adapter1": {"Address": GLib.Variant("s", "00:11")}}
        packed = {p: {i: {k: (v if isinstance(v, GLib.Variant) else
                              GLib.Variant({bool: "b", int: "u", str: "s", list: "as"}[type(v)], v))
                          for k, v in props.items()} for i, props in ifs.items()} for p, ifs in objs.items()}
        return GLib.Variant("(a{oa{sa{sv}}})", (packed,))


class Settings:
    def __init__(self, on=True):
        self.on = on

    def get_boolean(self, key):
        assert key == "announce-callers"
        return self.on


def headset_device(**over):
    d = {"Address": "AA:BB", "Connected": True, "Class": HEADSET, "UUIDs": [HFP], "Icon": "audio-headset"}
    d.update(over)
    return d


@pytest.fixture
def ann(mod, monkeypatch):
    glib = fakes.FakeGLib()
    monkeypatch.setattr(mod, "GLib", glib)
    bus = Bus()
    system = SystemBus([headset_device()])
    a = mod.Announcer(bus=bus, system_bus=system, settings=Settings())
    a.glib, a.bus_, a.system_ = glib, bus, system
    a.run = pactl(mod, monkeypatch)
    return a


def ring(a, path="/org/gnome/Calls/Call/1", name="Daniel Müller", number="+49151", state=5, inbound=True):
    a.calls[path] = {"state": state, "inbound": inbound, "name": name, "number": number}
    a.update()


def fire_timer(a):
    (sid,) = a.glib.of_kind("timeout")
    interval = a.glib.sources[sid][1]
    a.glib.fire(sid)
    return interval


def speaks(a):
    return [c[1] for c in a.bus_.calls if c[0] == "Speak"]


def test_rings_once_then_announces_and_repeats(ann):
    ring(ann)
    assert ann.bus_.calls == [("Prepare", None, "async")]   # the voice loads during the first ring
    assert fire_timer(ann) == 3000
    assert speaks(ann) == [("Daniel Müller is calling", {"role": "Ringtone"})]
    assert ["set-sink-input-mute", "7", "1"] in ann.run.calls     # the ringtone is muted meanwhile
    assert not ann.glib.of_kind("timeout")                        # nothing until it is said
    ann.bus_.finished(99)                                         # another speech: ignored
    assert ["set-sink-input-mute", "7", "0"] not in ann.run.calls
    ann.bus_.finished(42)
    assert ["set-sink-input-mute", "7", "0"] in ann.run.calls
    assert fire_timer(ann) == 4000                                # rings again, then again
    assert len(speaks(ann)) == 2


def test_answering_stops_at_once(ann):
    ring(ann)
    fire_timer(ann)
    ann.run.calls.clear()
    ring(ann, state=1)                      # answered (also with the headset's button)
    assert ann.bus_.calls[-1] == ("Stop", (42,))
    assert ["set-sink-input-mute", "7", "0"] in ann.run.calls
    assert ann.current is None and ann.speech_id is None
    ann.bus_.finished(42, False)            # the late Finished changes nothing
    assert not ann.glib.of_kind("timeout")


def test_declined_or_ended_during_the_first_ring(ann):
    ring(ann)
    ann.forget("/org/gnome/Calls/Call/1")
    assert not ann.glib.of_kind("timeout") and "Stop" not in ann.bus_.methods()
    ann.forget("/nothing")


def test_unknown_caller(ann):
    ring(ann, name="", number="")
    fire_timer(ann)
    assert speaks(ann)[0][0] == "Unknown caller"


@pytest.mark.parametrize("calls", [
    [dict(state=5, inbound=False)],                          # outgoing
    [dict(state=5), dict(state=1, path="/c/2")],             # a call already runs (waiting call)
    [dict(state=5), dict(state=5, path="/c/2")],             # two ringing
    [dict(state=3)],                                         # dialing
])
def test_no_announcement_for(ann, calls):
    for c in calls:
        ring(ann, **c)
    assert ann.current is None and not ann.glib.of_kind("timeout")


def test_switched_off(ann):
    ann.settings.on = False
    ring(ann)
    assert ann.current is None
    ann.settings.on = True
    ring(ann)
    fire_timer(ann)
    ann.settings.on = False
    ann.update()                              # switched off while it speaks
    assert ann.bus_.calls[-1] == ("Stop", (42,))


def test_switched_off_before_the_timer(ann):
    ring(ann)
    ann.settings.on = False
    fire_timer(ann)
    assert speaks(ann) == []


def test_no_ringtone_no_announcement(ann, monkeypatch, mod, capsys):
    pactl(mod, monkeypatch, inputs=[])         # silent or vibration profile
    ring(ann)
    fire_timer(ann)
    assert speaks(ann) == [] and not ann.glib.of_kind("timeout")
    assert "silenced" not in capsys.readouterr().err


def test_silenced_after_ringing(ann, monkeypatch, mod, capsys):
    ring(ann)
    fire_timer(ann)
    ann.bus_.finished(42)
    pactl(mod, monkeypatch, inputs=[])         # the ringing was silenced (power key)
    fire_timer(ann)
    assert len(speaks(ann)) == 1 and not ann.glib.of_kind("timeout")
    assert "ringing silenced" in capsys.readouterr().err


@pytest.mark.parametrize("devices,default", [
    ([headset_device(Class=0x240420)], None),              # the car
    ([headset_device(UUIDs=[HFP, PBAP_PCE])], None),       # hands-free that reads the phone book
    ([headset_device(Connected=False)], None),
    ([headset_device(Address="CC:DD")], None),
    ([headset_device()], "hal_output\n"),                  # the speaker is the output
])
def test_not_a_headset_waits_for_the_next_ring(ann, monkeypatch, mod, devices, default):
    ann.system_.devices = devices
    if default:
        ann.run = pactl(mod, monkeypatch, default=default)
    ring(ann)
    fire_timer(ann)
    assert speaks(ann) == []
    # Checked again at the next ring: a headset may connect meanwhile
    assert ann.glib.of_kind("timeout")


def test_bluez_missing(ann, capsys):
    ann.system_.fail = True
    ring(ann)
    fire_timer(ann)
    assert speaks(ann) == [] and "no BlueZ" in capsys.readouterr().err


def test_speak_fails(ann, capsys):
    ann.bus_.fail.add("Speak")
    ring(ann)
    fire_timer(ann)
    assert ["set-sink-input-mute", "7", "0"] in ann.run.calls
    assert ann.speech_id is None and "cannot speak" in capsys.readouterr().err


def test_stop_fails(ann, capsys):
    ring(ann)
    fire_timer(ann)
    ann.bus_.fail.add("Stop")
    ring(ann, state=7)
    assert ann.current is None and "cannot stop" in capsys.readouterr().err


def test_finished_after_the_call_ended(ann):
    ring(ann)
    fire_timer(ann)
    ann.speech_id = 42
    ann.current = None
    ann.bus_.finished(42)
    assert not ann.glib.of_kind("timeout")


# ---- Calls' object manager ----

class Proxy:
    def __init__(self, props, iface="org.gnome.Calls.Call"):
        self.props = props
        self.iface = iface

    def get_cached_property(self, name):
        v = self.props.get(name)
        return None if v is None else GLib.Variant({bool: "b", int: "u", str: "s"}[type(v)], v)

    def get_interface_name(self):
        return self.iface


class Obj:
    def __init__(self, path, props):
        self.path = path
        self.proxy = Proxy(props) if props is not None else None

    def get_object_path(self):
        return self.path

    def get_interface(self, iface):
        return self.proxy


class Manager:
    def __init__(self, objects):
        self.objects = objects
        self.handlers = {}

    def connect(self, signal, cb):
        self.handlers[signal] = cb

    def get_objects(self):
        return self.objects


def test_calls_from_the_object_manager(ann, mod, monkeypatch):
    obj = Obj("/org/gnome/Calls/Call/1", {"State": 5, "Inbound": True, "DisplayName": "Anna", "Id": "+49"})
    manager = Manager([obj, Obj("/org/gnome/Calls/x", None)])
    made = []

    class OMC:
        @staticmethod
        def new_sync(*args):
            made.append(args)
            return manager
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(Gio, DBusObjectManagerClient=OMC))
    ann.on_calls_appeared(ann.bus_, "org.gnome.Calls", ":1.3")
    assert ann.calls["/org/gnome/Calls/Call/1"] == {"state": 5, "inbound": True, "name": "Anna", "number": "+49"}
    assert ann.current == "/org/gnome/Calls/Call/1"
    # The name comes later (contacts are looked up after the call appears)
    changed = GLib.Variant("a{sv}", {"DisplayName": GLib.Variant("s", "Anna Bell")})
    manager.handlers["interface-proxy-properties-changed"](manager, obj, obj.proxy, changed, [])
    assert ann.calls["/org/gnome/Calls/Call/1"]["name"] == "Anna Bell"
    other = Proxy({}, iface="org.freedesktop.DBus.Properties")
    manager.handlers["interface-proxy-properties-changed"](manager, obj, other, changed, [])
    manager.handlers["interface-proxy-properties-changed"](manager, Obj("/unknown", {}), obj.proxy, changed, [])
    answered = GLib.Variant("a{sv}", {"State": GLib.Variant("u", 1)})
    manager.handlers["interface-proxy-properties-changed"](manager, obj, obj.proxy, answered, [])
    assert ann.current is None
    obj2 = Obj("/org/gnome/Calls/Call/2", {})
    manager.handlers["object-added"](manager, obj2)
    assert ann.calls["/org/gnome/Calls/Call/2"] == {"state": 0, "inbound": False, "name": "", "number": ""}
    manager.handlers["object-removed"](manager, obj2)
    assert "/org/gnome/Calls/Call/2" not in ann.calls
    ann.on_calls_vanished(ann.bus_, "org.gnome.Calls")
    assert ann.calls == {} and ann.manager is None


def test_no_object_manager(ann, mod, monkeypatch, capsys):
    class OMC:
        @staticmethod
        def new_sync(*args):
            raise GLib.Error("no calls")
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(Gio, DBusObjectManagerClient=OMC))
    ann.on_calls_appeared(ann.bus_, "org.gnome.Calls", ":1.3")
    assert ann.manager is None and "no object manager" in capsys.readouterr().err


def test_start(mod, monkeypatch):
    watched = []

    class Src:
        def __init__(self, found):
            self.found = found

        def lookup(self, schema, recursive):
            return self.found

    class SettingsT:
        @staticmethod
        def new(schema):
            assert schema == "org.plinius.speech"
            return made

    made = Settings()
    for found, expect in ((object(), made), (None, None)):
        gio = fakes.Proxy(Gio, SettingsSchemaSource=types.SimpleNamespace(get_default=lambda f=found: Src(f)),
                          Settings=SettingsT, bus_get_sync=lambda t, c: "system-bus",
                          bus_watch_name_on_connection=lambda *a: watched.append(a[1]))
        monkeypatch.setattr(mod, "Gio", gio)
        a = mod.Announcer(bus=Bus())
        a.start()
        assert a.settings == expect and a.system_bus == "system-bus"
        assert a.enabled() is True       # without the schema: on
    assert watched == ["org.gnome.Calls", "org.gnome.Calls"]


def test_default_session_bus(mod, monkeypatch):
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(Gio, bus_get_sync=lambda t, c: Bus()))
    assert isinstance(mod.Announcer().bus, Bus)
