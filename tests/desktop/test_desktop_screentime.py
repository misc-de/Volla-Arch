# plinius-screentime: the history file GNOME Settings' Wellbeing page reads (format checks,
# repair, trimming, ending a phase a crashed run left open) and when the phone counts as in use.
# The clock is faked; the file lives in tmp_path.

import json
import os
import signal as signal_module
import time as time_module
import types

import pytest
from gi.repository import Gio

import desktop_fakes as fakes
from conftest import PKG, load_script
from desktop_mainrun import run_as_main

SCREENTIME = PKG / "plinius-screentime" / "plinius-screentime.py"
DAY = 24 * 3600


@pytest.fixture(scope="module")
def mod():
    return load_script(SCREENTIME, "plinius_screentime_under_test")


@pytest.fixture
def clock(mod, monkeypatch):
    now = types.SimpleNamespace(t=1_800_000_000.0)
    monkeypatch.setattr(mod.time, "time", lambda: now.t)
    return now


def e(old, new, secs):
    return {"oldState": old, "newState": new, "wallTimeSecs": secs}


def on(secs):
    return e(0, 1, secs)


def off(secs):
    return e(1, 0, secs)


@pytest.fixture
def history(mod, tmp_path):
    return mod.History(str(tmp_path / "share" / "gnome-shell" / "session-active-history.json"))


def write(history, entries, mtime=None):
    os.makedirs(os.path.dirname(history.path), exist_ok=True)
    with open(history.path, "w") as f:
        json.dump(entries, f)
    if mtime is not None:
        os.utime(history.path, (mtime, mtime))


def stored(history):
    with open(history.path) as f:
        return json.load(f)


# ----- the format -----

@pytest.mark.parametrize("entry, ok", [
    (on(5), True), (off(5), True),
    (e(1, 1, 5), False), (e(0, 2, 5), False),
    (e(True, 0, 5), False),          # the Shell wants numbers, not booleans
    (e(0, 1, 5.5), False), (e(0, 1, "5"), False),
    ({"oldState": 0, "newState": 1}, False),
    (dict(on(5), extra=1), False), ([0, 1, 5], False), (None, False),
])
def test_valid_entry(mod, entry, ok):
    assert mod.valid_entry(entry) is ok


def test_repaired_keeps_a_good_history(mod):
    entries = [on(10), off(20), on(30), off(40)]
    assert mod.repaired(entries) == entries


def test_repaired_drops_a_phase_of_no_length(mod):
    assert mod.repaired([on(10), off(20), on(20), off(30)]) == [on(10), off(30)]
    assert mod.repaired([on(10), off(10), on(15)]) == [on(15)]


@pytest.mark.parametrize("entries, message", [
    ([on(10), off(5)], "backwards"),
    ([on(10), on(20)], "state not following"),
    ([on(10), e(0, 2, 20)], "malformed"),
])
def test_repaired_rejects(mod, entries, message):
    with pytest.raises(ValueError, match=message):
        mod.repaired(entries)


# ----- record and trim -----

def test_record_writes_transitions(mod, history, clock):
    history.record(mod.ACTIVE)
    clock.t += 60
    history.record(mod.ACTIVE)  # no change
    history.record(mod.INACTIVE)
    t = int(clock.t)
    assert stored(history) == [on(t - 60), off(t)]
    assert history.state() == mod.INACTIVE
    # Written compact and private, as the Shell does
    with open(history.path) as f:
        assert " " not in f.read()
    assert os.stat(history.path).st_mode & 0o777 == 0o600


def test_on_and_off_within_a_second_leaves_nothing(mod, history, clock):
    history.record(mod.ACTIVE)
    clock.t += 30
    history.record(mod.INACTIVE)
    clock.t += 0.4
    history.record(mod.ACTIVE)
    history.record(mod.INACTIVE)
    t = int(clock.t)
    assert history.entries == [on(t - 30), off(t)]
    # Each stored file is one the Settings accept
    assert mod.repaired(stored(history)) == stored(history)


def test_record_of_the_first_state_inactive_writes_nothing(mod, history, clock):
    history.record(mod.INACTIVE)
    assert not os.path.exists(history.path)


def test_trim_keeps_14_weeks_and_the_last_entry(mod, history, clock):
    now = int(clock.t)
    old = now - mod.KEEP_SECS - DAY
    history.entries = [on(old - 10), off(old), on(now - DAY), off(now - DAY + 600)]
    history.trim(now)
    assert history.entries == [on(now - DAY), off(now - DAY + 600)]
    history.entries = [on(old - 10), off(old)]
    history.trim(now)
    assert history.entries == [off(old)]


def test_trim_drops_entries_from_a_wrong_clock(mod, history, clock):
    now = int(clock.t)
    history.entries = [on(now - 100), off(now + 5000), on(now + 6000)]
    history.trim(now)
    assert history.entries == [on(now - 100)]


def test_store_of_nothing_deletes_the_file(mod, history, clock):
    write(history, [on(1)])
    history.entries = []
    history.store()
    assert not os.path.exists(history.path)
    history.delete()  # already gone: no error


# ----- load -----

def test_load_ends_a_phase_left_open_at_the_files_time(mod, history, clock):
    now = int(clock.t)
    write(history, [on(now - 7200), off(now - 3600), on(now - 600)], mtime=now - 540)
    history.load()
    assert history.entries[-1] == off(now - 540)
    assert stored(history) == history.entries
    assert history.state() == mod.INACTIVE


def test_load_ends_it_at_now_for_a_file_from_the_future(mod, history, clock):
    now = int(clock.t)
    write(history, [on(now - 600)], mtime=now + 3600)
    history.load()
    assert history.entries == [on(now - 600), off(now)]


def test_load_drops_a_phase_that_ended_as_it_began(mod, history, clock):
    now = int(clock.t)
    write(history, [on(now - 900), off(now - 800), on(now - 100)], mtime=now - 100)
    history.load()
    assert history.entries == [on(now - 900), off(now - 800)]
    assert stored(history) == history.entries


def test_load_repairs_and_stores(mod, history, clock):
    now = int(clock.t)
    entries = [on(now - 900), off(now - 800), on(now - 800), off(now - 700)]
    write(history, entries, mtime=now)
    history.load()
    assert stored(history) == [on(now - 900), off(now - 700)]


def test_load_of_a_good_closed_file_does_not_rewrite_it(mod, history, clock):
    now = int(clock.t)
    write(history, [on(now - 900), off(now - 800)], mtime=now - 50)
    history.load()
    assert int(os.stat(history.path).st_mtime) == now - 50


@pytest.mark.parametrize("content", ["{not json", '{"a": 1}', json.dumps([on(5), on(6)])])
def test_load_sets_an_unusable_file_aside(mod, history, clock, content):
    os.makedirs(os.path.dirname(history.path))
    with open(history.path, "w") as f:
        f.write(content)
    history.load()
    assert history.entries == []
    assert not os.path.exists(history.path)
    with open(history.path + ".corrupt") as f:
        assert f.read() == content


def test_load_without_a_file(mod, history, clock):
    history.load()
    assert history.entries == [] and not os.path.exists(history.path)


def test_touch_renews_the_time(mod, history, clock):
    write(history, [on(1)], mtime=1000)
    history.touch()
    assert os.stat(history.path).st_mtime > 1000


# ----- in use: display on and unlocked -----

@pytest.fixture
def service(mod, history, clock, monkeypatch):
    glib = fakes.FakeGLib()
    monkeypatch.setattr(mod, "GLib", glib)
    s = mod.ScreenTime.__new__(mod.ScreenTime)
    s.history = history
    s.display_on = False
    s.locked = True
    s.heartbeat = 0
    s.enabled = True
    s.glib = glib
    return s


def test_in_use_only_with_the_display_on_and_unlocked(mod, service, clock):
    service.on_display(None, None, None, None, None, ("on",))
    assert service.history.state() == mod.INACTIVE
    service.on_lock(None, None, None, None, None, (False,))
    assert service.history.state() == mod.ACTIVE
    (hb,) = service.glib.of_kind("seconds")
    assert service.glib.sources[hb][1] == mod.HEARTBEAT_SECS
    service.glib.fire(hb)  # the heartbeat renews the file and keeps going
    assert hb in service.glib.sources
    # A dimmed display is still on
    service.on_display(None, None, None, None, None, ("dimmed",))
    assert service.history.state() == mod.ACTIVE
    service.on_display(None, None, None, None, None, ("off",))
    assert service.history.state() == mod.INACTIVE
    assert service.glib.sources == {} and service.heartbeat == 0


def test_services_going_away_count_as_not_in_use(mod, service):
    service.display_on, service.locked = True, False
    service.update()
    service.screensaver_vanished(None, None)
    assert service.history.state() == mod.INACTIVE
    service.locked = False
    service.update()
    service.mce_vanished(None, None)
    assert service.history.state() == mod.INACTIVE


class Bus:
    def __init__(self, reply):
        self.reply = reply

    def call_sync(self, *args):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_appearing_services_are_asked(mod, service):
    service.mce_appeared(Bus(("on",)), None, None)
    service.screensaver_appeared(Bus((False,)), None, None)
    assert service.history.state() == mod.ACTIVE
    service.screensaver_appeared(Bus(fakes.gerror("timeout")), None, None)
    assert service.locked is True and service.history.state() == mod.INACTIVE
    service.mce_appeared(Bus(fakes.gerror("timeout")), None, None)
    assert service.display_on is False


class Settings:
    def __init__(self, value):
        self.value = value

    def get_boolean(self, key):
        return self.value


def test_switching_the_history_off_deletes_it(mod, service, clock):
    service.settings = Settings(True)
    service.display_on, service.locked = True, False
    service.update()
    assert os.path.exists(service.history.path)
    service.settings.value = False
    service.on_setting(None, None)
    assert not os.path.exists(service.history.path) and service.glib.sources == {}
    # Off: nothing is recorded any more
    clock.t += 100
    service.on_lock(None, None, None, None, None, (True,))
    assert not os.path.exists(service.history.path)
    service.settings.value = True
    service.on_setting(None, None)
    service.on_lock(None, None, None, None, None, (False,))
    assert service.history.state() == mod.ACTIVE


def test_stop_ends_the_phase(mod, service, clock):
    service.display_on, service.locked = True, False
    service.update()
    clock.t += 5
    service.stop()
    assert stored(service.history)[-1] == off(int(clock.t))


# ----- files that cannot be written -----

def test_an_unusable_file_that_cannot_be_set_aside_stays(mod, history, clock, capsys):
    os.makedirs(os.path.dirname(history.path))
    with open(history.path, "w") as f:
        f.write("{not json")
    os.chmod(os.path.dirname(history.path), 0o500)  # no renaming in it
    try:
        history.load()
    finally:
        os.chmod(os.path.dirname(history.path), 0o700)
    assert history.entries == []
    assert open(history.path).read() == "{not json"
    assert not os.path.exists(history.path + ".corrupt")
    err = capsys.readouterr().err
    assert "cannot use" in err and "cannot set it aside:" in err


def test_store_and_delete_failing_are_logged(mod, tmp_path, clock, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("")
    history = mod.History(str(blocker / "gnome-shell" / "history.json"))
    history.record(mod.ACTIVE)  # its directory cannot be made
    assert history.entries == [on(int(clock.t))]  # kept in memory all the same
    assert "cannot store the history:" in capsys.readouterr().err

    folder = tmp_path / "a-directory"
    folder.mkdir()
    history = mod.History(str(folder))
    history.entries = [on(1)]
    history.delete()
    assert history.entries == [] and folder.is_dir()
    assert "cannot delete the history:" in capsys.readouterr().err


def test_touch_without_a_file_is_logged(mod, history, capsys):
    history.touch()
    assert not os.path.exists(history.path)
    assert "cannot renew the history's time:" in capsys.readouterr().err


def test_setting_unchanged_does_nothing(mod, service):
    service.settings = Settings(True)
    service.on_setting(None, None)
    assert service.enabled is True and not os.path.exists(service.history.path)
    # Off while nothing runs: no heartbeat to stop
    service.settings.value = False
    service.on_setting(None, None)
    assert service.enabled is False and service.glib.removed == []
    service.stop()  # off: nothing is written
    assert not os.path.exists(service.history.path)


# ----- the program as the session starts it -----

class MainSettings:
    def __init__(self, enabled):
        self.enabled = enabled
        self.handlers = []

    def get_boolean(self, key):
        assert key == "history-enabled"
        return self.enabled

    def connect(self, signal, handler):
        self.handlers.append((signal, handler))


class MainBus:
    """A bus that records subscriptions and answers the state queries."""

    def __init__(self, replies):
        self.replies = replies
        self.subscriptions = []

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, handler):
        self.subscriptions.append((sender, iface, member, path, handler))
        return len(self.subscriptions)

    def call_sync(self, name, path, iface, method, *rest):
        return self.replies[method]


def run_service(mod, tmp_path, monkeypatch, enabled, during_run):
    """plinius-screentime run as a program: buses, settings and the main loop faked; during_run
    gets what the program set up and plays what happens while the loop runs."""
    now = types.SimpleNamespace(t=1_800_000_000.0)
    monkeypatch.setattr(time_module, "time", lambda: now.t)
    setup = types.SimpleNamespace(now=now, watches=[], signals={}, settings=MainSettings(enabled),
                                  system=MainBus({"get_display_status": ("on",)}),
                                  session=MainBus({"GetActive": (False,)}), loops=[])

    class Loop:
        def __init__(self):
            self.quit_called = False
            setup.loops.append(self)

        def run(self):
            during_run(setup)

        def quit(self):
            self.quit_called = True

    gio = fakes.Proxy(
        Gio, Settings=types.SimpleNamespace(new=lambda schema: setup.settings),
        bus_get_sync=lambda kind, cancellable: (setup.system if kind == Gio.BusType.SYSTEM
                                                else setup.session),
        bus_watch_name_on_connection=lambda bus, name, flags, up, down: setup.watches.append(
            (bus, name, up, down)))
    glib = fakes.FakeGLib(MainLoop=Loop, get_user_data_dir=lambda: str(tmp_path / "data"))
    unix = types.SimpleNamespace(
        signal_add=lambda prio, sig, handler: setup.signals.setdefault(sig, handler))
    setup.glib = glib
    setup.path = tmp_path / "data" / "gnome-shell" / "session-active-history.json"
    assert run_as_main(SCREENTIME, Gio=gio, GLib=glib, GLibUnix=unix) is None
    return setup


def test_program_records_a_phase_until_sigterm(mod, tmp_path, monkeypatch):
    def during_run(setup):
        assert setup.settings.handlers[0][0] == "changed::history-enabled"
        assert [s[2] for s in setup.system.subscriptions] == ["display_status_ind"]
        assert [s[2] for s in setup.session.subscriptions] == ["ActiveChanged"]
        assert [w[1] for w in setup.watches] == ["com.nokia.mce", "org.gnome.ScreenSaver"]
        assert set(setup.signals) == {signal_module.SIGTERM, signal_module.SIGINT,
                                      signal_module.SIGHUP}
        for bus, name, up, down in setup.watches:
            up(bus, name, ":1.7")
        assert len(setup.glib.of_kind("seconds")) == 1  # the heartbeat runs
        setup.now.t += 300
        assert setup.signals[signal_module.SIGTERM]() is False
        assert setup.loops[0].quit_called

    setup = run_service(mod, tmp_path, monkeypatch, True, during_run)
    t = int(setup.now.t)
    assert json.loads(setup.path.read_text()) == [on(t - 300), off(t)]


def test_program_with_the_history_off_deletes_it(mod, tmp_path, monkeypatch):
    path = tmp_path / "data" / "gnome-shell" / "session-active-history.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([on(1), off(2)]))

    def during_run(setup):
        assert not setup.path.exists()
        for bus, name, up, down in setup.watches:
            up(bus, name, ":1.7")
        setup.signals[next(iter(setup.signals))]()

    setup = run_service(mod, tmp_path, monkeypatch, False, during_run)
    assert not setup.path.exists() and setup.glib.sources == {}
