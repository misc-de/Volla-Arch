#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""plinius-callfocus: music and videos make way for calls and the microphone.

As on Android: when a call rings or is dialled, or an app records (dictation, a voice message),
whatever plays pauses, and the call or the recording has the sound to itself; when the last call
or recording ends, what was paused plays on.

- Players with MPRIS (music apps, Firefox, video players) are paused and later resumed, but only
  those this paused and only if they are still paused then (a player the user touched during the
  call is left alone).
- Other playback streams (apps without MPRIS) are muted through pipewire-pulse and unmuted
  afterwards. Sounds that belong to calls and alerts (roles phone, communication, ringtone,
  notification, alarm, event, emergency; feedbackd, Calls, the audio bridge, loopbacks) stay.
  WirePlumber remembers a stream's mute per app, so the muted apps are written to a file in the
  runtime directory and unmuted at the next start should this die during a call.

The calls come from GNOME Calls (org.gnome.Calls, object manager at /org/gnome/Calls: one
org.gnome.Calls.Call object per call, mobile and SIP alike). Calls 51 states: 1 active, 2 held,
3 dialing, 4 alerting, 5 incoming, 6 waiting, 7 disconnected (seen in its log); 1 to 6 count as
a call.

Recordings are the capture streams of pipewire-pulse (source outputs), without the audio
bridge's own, loopbacks, monitors and level meters. A recording from a Bluetooth headset switches
it to hands-free, and the A2DP stream breaks off (Emilia stayed paused after dictating,
2026-11-12); the players resume only once the bridge has given hands-free back. The player often
pauses itself before the recording is seen, so players that stopped playing just before count as
paused by this. Other streams are not muted for a recording.

Runs in the Phosh session (XDG autostart).
"""

import json
import os
import subprocess
import sys
import time

from gi.repository import Gio, GLib

CALLS = "org.gnome.Calls"
CALLS_PATH = "/org/gnome/Calls"
CALL_IFACE = "org.gnome.Calls.Call"
MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

# Roles and programs whose sound belongs to the call or must still be heard
KEEP_ROLES = {"phone", "communication", "ringtone", "notification", "alarm", "event", "emergency"}
KEEP_BINARIES = {"feedbackd", "gnome-calls", "audio-hal-bridge", "pipewire", "wireplumber"}
RESUME_DELAY_MS = 1000  # the audio bridge leaves call mode first
# The audio bridge keeps hands-free 1.5 s after the last recording, then the headset needs a
# moment back in A2DP; played earlier, the music would go to the hands-free sink or break off again
RECORDING_RESUME_DELAY_MS = 3000
RECORDING_SETTLE_MS = 300  # pactl events come in bursts
# A player paused this shortly before a recording was seen paused for it: the headset's switch to
# hands-free (AVRCP pause, or the A2DP stream breaking off) is often quicker than pactl
RECENT_PAUSE_S = 3.0

STATE_FILE = os.path.join(GLib.get_user_runtime_dir(), "plinius-callfocus.json")


def log(*args):
    print("plinius-callfocus:", *args, file=sys.stderr, flush=True)


def in_call_state(state):
    return 1 <= state <= 6


class CallFocus:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.calls = {}           # object path -> state
        self.paused = []          # MPRIS bus names this paused
        self.muted = []           # (sink input index, application) this muted
        self.focused = False      # a call or a recording has the sound
        self.recording = False    # an app records
        self.recorded = False     # a recording was part of this focus
        self.call_muted = False   # a call of this focus muted the streams
        self.resume_source = 0
        self.settle_source = 0
        self.last_pause = {}      # MPRIS unique name -> monotonic time it went from Playing to Paused
        self.manager = None
        self.unmute_leftovers()
        Gio.bus_watch_name_on_connection(self.bus, CALLS, Gio.BusNameWatcherFlags.NONE,
                                         self.on_calls_appeared, self.on_calls_vanished)
        self.bus.signal_subscribe(None, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                  MPRIS_PATH, PLAYER_IFACE, Gio.DBusSignalFlags.MATCH_ARG0_NAMESPACE,
                                  self.on_player_changed)
        self.watch_recordings()

    # ---- calls ----

    def on_calls_appeared(self, bus, name, owner):
        # Synchronous: the asynchronous constructor never called back from Python (its
        # arguments map differently in PyGObject), so no call was ever seen (2026-11-02)
        try:
            self.manager = Gio.DBusObjectManagerClient.new_sync(
                self.bus, Gio.DBusObjectManagerClientFlags.DO_NOT_AUTO_START, CALLS, CALLS_PATH,
                None, None)
        except GLib.Error as e:
            log("no object manager of Calls:", e.message)
            return
        self.manager.connect("object-added", lambda m, o: self.track(o))
        self.manager.connect("object-removed", lambda m, o: self.forget(o.get_object_path()))
        self.manager.connect("interface-proxy-properties-changed", self.on_properties)
        for obj in self.manager.get_objects():
            self.track(obj)

    def on_calls_vanished(self, bus, name):
        self.manager = None
        self.calls.clear()
        self.update()

    def track(self, obj):
        proxy = obj.get_interface(CALL_IFACE)
        if proxy:
            self.calls[obj.get_object_path()] = self.state_of(proxy)
            self.update()

    def forget(self, path):
        if self.calls.pop(path, None) is not None:
            self.update()

    def on_properties(self, manager, obj, proxy, changed, invalidated):
        if proxy.get_interface_name() == CALL_IFACE and "State" in changed.keys():
            self.calls[obj.get_object_path()] = changed["State"]
            self.update()

    @staticmethod
    def state_of(proxy):
        value = proxy.get_cached_property("State")
        return value.unpack() if value is not None else 0

    def update(self):
        call = any(in_call_state(s) for s in self.calls.values())
        if self.recording:
            self.recorded = True
        if (call or self.recording) and not self.focused:
            self.focused = True
            if self.resume_source:
                GLib.source_remove(self.resume_source)  # a new call or recording right after
                self.resume_source = 0
                if call:
                    # The focus may have been taken for a recording, which mutes nothing
                    self.mute_for_call()
            else:
                self.take_focus("call" if call else "recording")
        elif call and self.focused and not self.call_muted:
            # A call while the focus is held for a recording, which muted nothing
            self.mute_for_call()
        elif not (call or self.recording) and self.focused:
            self.focused = False
            delay = RECORDING_RESUME_DELAY_MS if self.recorded else RESUME_DELAY_MS
            self.resume_source = GLib.timeout_add(delay, self.give_back)

    def on_player_changed(self, bus, sender, path, iface, signal, params):
        status = params.unpack()[1].get("PlaybackStatus")
        if status == "Paused":
            self.last_pause[sender] = time.monotonic()
        elif status is not None:
            self.last_pause.pop(sender, None)

    # ---- recordings ----

    def watch_recordings(self):
        """Follows pactl subscribe; starts it again should pipewire-pulse restart"""
        try:
            proc = Gio.Subprocess.new(["pactl", "subscribe"], Gio.SubprocessFlags.STDOUT_PIPE)
        except GLib.Error as e:
            log("cannot follow the recordings:", e.message)
            GLib.timeout_add_seconds(10, self.restart_watch)
            return
        lines = Gio.DataInputStream.new(proc.get_stdout_pipe())
        lines.read_line_async(GLib.PRIORITY_DEFAULT, None, self.on_event, proc)
        self.check_recording()

    def restart_watch(self):
        self.watch_recordings()
        return GLib.SOURCE_REMOVE

    def on_event(self, lines, result, proc):
        try:
            line, _ = lines.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
        if line is None:  # pactl ended
            proc.force_exit()
            GLib.timeout_add_seconds(2, self.restart_watch)
            return
        if "source-output" in line and not self.settle_source:
            self.settle_source = GLib.timeout_add(RECORDING_SETTLE_MS, self.on_settled)
        lines.read_line_async(GLib.PRIORITY_DEFAULT, None, self.on_event, proc)

    def on_settled(self):
        self.settle_source = 0
        self.check_recording()
        return GLib.SOURCE_REMOVE

    def check_recording(self):
        apps = self.recording_apps()
        recording = bool(apps)
        if recording != self.recording:
            log("recording:", apps if recording else "over")
            self.recording = recording
            self.update()

    @staticmethod
    def source_outputs():
        try:
            out = subprocess.run(["pactl", "-f", "json", "list", "source-outputs"],
                                 capture_output=True, text=True, timeout=5, check=True).stdout
            return json.loads(out)
        except (OSError, subprocess.SubprocessError, ValueError):
            return []

    @staticmethod
    def hidden_sources():
        """Indices of the sources marked plinius.hidden: no microphone, such as the FM tuner's
        sound (audio-hal-bridge), which the radio app records to play it"""
        try:
            out = subprocess.run(["pactl", "-f", "json", "list", "sources"],
                                 capture_output=True, text=True, timeout=5, check=True).stdout
            return {s.get("index") for s in json.loads(out)
                    if s.get("properties", {}).get("plinius.hidden") == "true"}
        except (OSError, subprocess.SubprocessError, ValueError):
            return set()

    def recording_apps(self):
        apps = []
        outputs = self.source_outputs()
        hidden = self.hidden_sources() if outputs else set()
        for s in outputs:
            props = s.get("properties", {})
            binary = props.get("application.process.binary", "")
            if (s.get("corked") or binary in KEEP_BINARIES or "node.link-group" in props
                    or s.get("source") in hidden
                    or props.get("node.virtual") == "true" or props.get("stream.monitor") == "true"
                    or props.get("stream.capture.sink") == "true"
                    or props.get("media.name", "").lower() == "peak detect"):
                continue
            apps.append(props.get("application.name") or binary or str(s.get("index")))
        return apps

    # ---- the sound ----

    def take_focus(self, reason):
        now = time.monotonic()
        for name in self.players():
            status = self.player_status(name)
            if status == "Playing" and self.player_call(name, "Pause"):
                self.paused.append(name)
            elif (reason == "recording" and status == "Paused"
                  and now - self.last_pause.get(self.owner(name), -1e9) < RECENT_PAUSE_S):
                self.paused.append(name)  # paused itself as the headset went hands-free
        if reason == "call":
            self.call_muted = True
            self.mute_streams()
        self.save_state()
        log(f"{reason}: paused", self.paused or "nothing", "muted", [a for _, a in self.muted] or "nothing")

    def mute_streams(self):
        """Mutes the playing streams of apps without MPRIS for a call; True if it muted one"""
        done = {index for index, _ in self.muted}
        before = len(self.muted)
        for index, app in self.playing_streams():
            if index not in done and self.set_mute(index, True):
                self.muted.append((index, app))
        return len(self.muted) > before

    def mute_for_call(self):
        """A call while the focus is already held: mute what plays and keep it in the state file"""
        self.call_muted = True
        if self.mute_streams():
            self.save_state()
            log("call: muted", [a for _, a in self.muted])

    def give_back(self):
        self.resume_source = 0
        for index, _ in self.muted:
            self.set_mute(index, False)
        resumed = []
        for name in self.paused:
            if self.player_status(name) == "Paused" and self.player_call(name, "Play"):
                resumed.append(name)
        log("over: resumed", resumed or "nothing", "unmuted", [a for _, a in self.muted] or "nothing")
        self.paused = []
        self.muted = []
        self.recorded = self.call_muted = False
        self.save_state()
        return GLib.SOURCE_REMOVE

    # MPRIS

    def players(self):
        try:
            names = self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                       "org.freedesktop.DBus", "ListNames", None,
                                       GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error:
            return []
        # playerctld only passes on to the player it follows, which is in the list itself
        return [n for n in names.unpack()[0] if n.startswith(MPRIS_PREFIX) and n != MPRIS_PREFIX + "playerctld"]

    def owner(self, name):
        try:
            reply = self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                       "org.freedesktop.DBus", "GetNameOwner", GLib.Variant("(s)", (name,)),
                                       GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, 2000, None)
            return reply.unpack()[0]
        except GLib.Error:
            return None

    def player_status(self, name):
        try:
            reply = self.bus.call_sync(name, MPRIS_PATH, "org.freedesktop.DBus.Properties", "Get",
                                       GLib.Variant("(ss)", (PLAYER_IFACE, "PlaybackStatus")),
                                       GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 2000, None)
            return reply.unpack()[0]
        except GLib.Error:
            return None

    def player_call(self, name, method):
        try:
            self.bus.call_sync(name, MPRIS_PATH, PLAYER_IFACE, method, None, None,
                               Gio.DBusCallFlags.NONE, 2000, None)
            return True
        except GLib.Error as e:
            log(f"{method} on {name} failed:", e.message)
            return False

    # Streams without MPRIS, through pipewire-pulse

    @staticmethod
    def sink_inputs():
        try:
            out = subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"], capture_output=True,
                                 text=True, timeout=5, check=True).stdout
            return json.loads(out)
        except (OSError, subprocess.SubprocessError, ValueError):
            return []

    def playing_streams(self):
        streams = []
        for s in self.sink_inputs():
            props = s.get("properties", {})
            role = props.get("media.role", "").lower()
            binary = props.get("application.process.binary", "")
            if (s.get("mute") or s.get("corked") or role in KEEP_ROLES or binary in KEEP_BINARIES
                    or "node.link-group" in props or props.get("node.virtual") == "true"):
                continue
            app = props.get("application.name") or binary or str(s.get("index"))
            streams.append((s["index"], app))
        return streams

    @staticmethod
    def set_mute(index, mute):
        try:
            subprocess.run(["pactl", "set-sink-input-mute", str(index), "1" if mute else "0"],
                           capture_output=True, timeout=5, check=True)
            return True
        except (OSError, subprocess.SubprocessError):
            return False  # the stream is gone

    def save_state(self):
        try:
            if self.muted:
                with open(STATE_FILE, "w") as f:
                    json.dump([app for _, app in self.muted], f)
            elif os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
        except OSError as e:
            log("cannot keep the muted apps:", e)

    def unmute_leftovers(self):
        """Apps left muted by an earlier run that ended during a call"""
        try:
            with open(STATE_FILE) as f:
                apps = set(json.load(f))
        except (OSError, ValueError):
            return
        for s in self.sink_inputs():
            props = s.get("properties", {})
            app = props.get("application.name") or props.get("application.process.binary", "")
            if s.get("mute") and app in apps:
                self.set_mute(s["index"], False)
        log("unmuted after an earlier run:", sorted(apps))
        os.remove(STATE_FILE)


def main():
    CallFocus()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
