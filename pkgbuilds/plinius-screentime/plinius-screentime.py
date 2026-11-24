#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""plinius-screentime: records screen time for the Wellbeing page of the Settings.

The page only reads and draws ~/.local/share/gnome-shell/session-active-history.json. GNOME Shell
writes that file (js/misc/timeLimitsManager.js), so under Phosh it never came to exist and the page
stayed empty. This writes the same file in the same format: a JSON array of
{"oldState", "newState", "wallTimeSecs"} with the states 0 inactive and 1 active, wallTimeSecs not
decreasing, kept for 14 weeks. As in the Shell, the switch history-enabled of
org.gnome.desktop.screen-time-limits turns it off and deletes the history.

The phone counts as in use while the display is on and not locked, as on Android. The Shell's own
signals would not do: nothing sets logind's IdleHint here, and plinius-suspend suspends past logind,
so PrepareForSleep never comes. A phone only suspends with the display off, which already ended
the active phase.

- Display: com.nokia.mce on the system bus (plinius-mce). The always-on display counts as off.
- Lock: org.gnome.ScreenSaver on the session bus (Phosh). While either service is missing, the
  phone counts as not in use: a restart of the shell shows the lock screen anyway.

A phase has to end in the file, or the page would count the time until the next start as use. That
happens at SIGTERM. For a crash or a power cut, the file's modification time is renewed once a
minute during a phase and ends it at the next start.

Runs in the Phosh session (XDG autostart).
"""

import json
import os
import signal
import sys
import time

from gi.repository import Gio, GLib, GLibUnix

MCE = "com.nokia.mce"
MCE_REQUEST_PATH = "/com/nokia/mce/request"
MCE_REQUEST_IFACE = "com.nokia.mce.request"
MCE_SIGNAL_PATH = "/com/nokia/mce/signal"
MCE_SIGNAL_IFACE = "com.nokia.mce.signal"
SCREENSAVER = "org.gnome.ScreenSaver"
SCREENSAVER_PATH = "/org/gnome/ScreenSaver"
SETTINGS_SCHEMA = "org.gnome.desktop.screen-time-limits"
SETTINGS_KEY = "history-enabled"

INACTIVE = 0
ACTIVE = 1
KEEP_SECS = 14 * 7 * 24 * 60 * 60  # HISTORY_THRESHOLD_SECONDS of the Shell
HEARTBEAT_SECS = 60
CALL_TIMEOUT_MS = 2000

HISTORY_FILE = os.path.join(GLib.get_user_data_dir(), "gnome-shell", "session-active-history.json")


def log(*args):
    print("plinius-screentime:", *args, file=sys.stderr, flush=True)


def valid_entry(entry):
    """The checks the Shell makes when it loads the file, except the order of the times."""
    if not isinstance(entry, dict) or set(entry) != {"oldState", "newState", "wallTimeSecs"}:
        return False
    old, new, secs = entry["oldState"], entry["newState"], entry["wallTimeSecs"]
    return (type(old) is int and type(new) is int and type(secs) is int
            and {old, new} == {INACTIVE, ACTIVE})


def repaired(entries):
    """The entries with strictly increasing times, as the Settings demand them: the Wellbeing page
    rejects the whole file ("Screen Time Unavailable") as soon as one time equals the one before.
    0.1-1 wrote such a pair when a phase of a crashed run had ended within the second it began.
    A phase of no length is left out; any other time going backwards makes the file unusable."""
    kept = []
    for entry in entries:
        if not valid_entry(entry):
            raise ValueError(f"malformed entry {entry!r}")
        if kept and entry["wallTimeSecs"] <= kept[-1]["wallTimeSecs"]:
            if entry["wallTimeSecs"] == kept[-1]["wallTimeSecs"] \
                    and entry["newState"] == kept[-1]["oldState"]:
                kept.pop()  # on and off within a second
                continue
            raise ValueError(f"time going backwards at {entry!r}")
        if kept and entry["oldState"] != kept[-1]["newState"]:
            raise ValueError(f"state not following on at {entry!r}")
        kept.append(entry)
    return kept


class History:
    """The state transitions and their file."""

    def __init__(self, path):
        self.path = path
        self.entries = []

    def load(self):
        """Reads the file and ends a phase that an earlier run left open."""
        try:
            with open(self.path, encoding="utf-8") as f:
                entries = json.load(f)
            modified = int(os.stat(self.path).st_mtime)
            if not isinstance(entries, list):
                raise ValueError("not an array")
            fixed = repaired(entries)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            # Kept for a look: the next store would overwrite it
            log(f"cannot use {self.path}: {e}")
            try:
                os.replace(self.path, self.path + ".corrupt")
            except OSError as e2:
                log("cannot set it aside:", e2)
            return
        self.entries = fixed
        changed = len(fixed) != len(entries)
        if changed:
            log(f"left out {(len(entries) - len(fixed)) // 2} phase(s) of no length")
        now = int(time.time())
        self.trim(now)
        if self.state() == ACTIVE:
            start = self.entries[-1]["wallTimeSecs"]
            end = min(modified, now)
            if end > start:
                self.entries.append({"oldState": ACTIVE, "newState": INACTIVE, "wallTimeSecs": end})
                log(f"ended a phase of an earlier run, {end - start} s")
            else:
                self.entries.pop()  # it ended within the second it began
                log("left out a phase of an earlier run of no length")
            changed = True
        if changed:
            self.store()

    def state(self):
        return self.entries[-1]["newState"] if self.entries else INACTIVE

    def trim(self, now):
        """Drops what is too old, but keeps the last entry as the starting point. Entries in the
        future can only come from a clock that was wrong, and dropping them keeps the times
        increasing."""
        self.entries = [e for e in self.entries if e["wallTimeSecs"] <= now]
        last = self.entries[-1] if self.entries else None
        self.entries = [e for e in self.entries if e["wallTimeSecs"] >= now - KEEP_SECS or e is last]

    def record(self, state):
        now = int(time.time())
        self.trim(now)
        old = self.state()
        if old == state:
            return
        if self.entries and self.entries[-1]["wallTimeSecs"] == now \
                and self.entries[-1]["oldState"] == state:
            self.entries.pop()  # on and off within a second
        else:
            self.entries.append({"oldState": old, "newState": state, "wallTimeSecs": now})
        self.store()

    def store(self):
        if not self.entries:
            self.delete()
            return
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.entries, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            log("cannot store the history:", e)

    def delete(self):
        self.entries = []
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        except OSError as e:
            log("cannot delete the history:", e)

    def touch(self):
        try:
            os.utime(self.path)
        except OSError as e:
            log("cannot renew the history's time:", e)


class ScreenTime:
    def __init__(self):
        self.history = History(HISTORY_FILE)
        self.display_on = False
        self.locked = True
        self.heartbeat = 0
        self.settings = Gio.Settings.new(SETTINGS_SCHEMA)
        self.enabled = self.settings.get_boolean(SETTINGS_KEY)
        if self.enabled:
            self.history.load()
        else:
            self.history.delete()
        self.settings.connect(f"changed::{SETTINGS_KEY}", self.on_setting)

        self.system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.system.signal_subscribe(MCE, MCE_SIGNAL_IFACE, "display_status_ind", MCE_SIGNAL_PATH,
                                     None, Gio.DBusSignalFlags.NONE, self.on_display)
        self.session.signal_subscribe(SCREENSAVER, SCREENSAVER, "ActiveChanged", SCREENSAVER_PATH,
                                      None, Gio.DBusSignalFlags.NONE, self.on_lock)
        Gio.bus_watch_name_on_connection(self.system, MCE, Gio.BusNameWatcherFlags.NONE,
                                         self.mce_appeared, self.mce_vanished)
        Gio.bus_watch_name_on_connection(self.session, SCREENSAVER, Gio.BusNameWatcherFlags.NONE,
                                         self.screensaver_appeared, self.screensaver_vanished)

    def update(self):
        if not self.enabled:
            return
        active = self.display_on and not self.locked
        self.history.record(ACTIVE if active else INACTIVE)
        if active and not self.heartbeat:
            self.heartbeat = GLib.timeout_add_seconds(HEARTBEAT_SECS, self.on_heartbeat)
        elif not active and self.heartbeat:
            GLib.source_remove(self.heartbeat)
            self.heartbeat = 0

    def on_heartbeat(self):
        self.history.touch()
        return GLib.SOURCE_CONTINUE

    def on_setting(self, _settings, _key):
        enabled = self.settings.get_boolean(SETTINGS_KEY)
        if enabled == self.enabled:
            return
        if enabled:
            self.enabled = True
            self.update()
        else:
            self.enabled = False
            if self.heartbeat:
                GLib.source_remove(self.heartbeat)
                self.heartbeat = 0
            self.history.delete()

    def on_display(self, _bus, _sender, _path, _iface, _signal, params):
        self.display_on = params[0] != "off"
        self.update()

    def on_lock(self, _bus, _sender, _path, _iface, _signal, params):
        self.locked = params[0]
        self.update()

    def mce_appeared(self, bus, _name, _owner):
        try:
            reply = bus.call_sync(MCE, MCE_REQUEST_PATH, MCE_REQUEST_IFACE, "get_display_status",
                                  None, GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE,
                                  CALL_TIMEOUT_MS, None)
            self.display_on = reply[0] != "off"
        except GLib.Error as e:
            log("cannot read the display state:", e.message)
            self.display_on = False
        self.update()

    def mce_vanished(self, _bus, _name):
        self.display_on = False
        self.update()

    def screensaver_appeared(self, bus, _name, _owner):
        try:
            reply = bus.call_sync(SCREENSAVER, SCREENSAVER_PATH, SCREENSAVER, "GetActive",
                                  None, GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE,
                                  CALL_TIMEOUT_MS, None)
            self.locked = reply[0]
        except GLib.Error as e:
            log("cannot read the lock state:", e.message)
            self.locked = True
        self.update()

    def screensaver_vanished(self, _bus, _name):
        self.locked = True
        self.update()

    def stop(self):
        if self.enabled:
            self.history.record(INACTIVE)


def main():
    service = ScreenTime()
    loop = GLib.MainLoop()

    def on_signal():
        service.stop()
        loop.quit()
        return GLib.SOURCE_REMOVE

    for sig in signal.SIGTERM, signal.SIGINT, signal.SIGHUP:
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, sig, on_signal)
    loop.run()


if __name__ == "__main__":
    main()
