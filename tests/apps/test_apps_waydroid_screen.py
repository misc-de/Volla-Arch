# waydroid-plinius-screen: Android's screen follows the phone's lock. The logind proxies are
# stand-ins (cached properties, the g-properties-changed handler); waydroid is a stub whose
# status comes from FAKE_STATE and which logs its calls.

import weakref

import pytest

from conftest import PKG, load_script

SCRIPT = PKG / "waydroid-plinius" / "waydroid-plinius-screen"

WAYDROID = r'''
if [ "$1" = status ]; then
	[ -n "$FAKE_STATUS_FAILS" ] && exit 1
	printf 'Session:\tRUNNING\nContainer:\t%s\nVendor type:\tHALIUM_14\n' "$FAKE_STATE"
fi
[ "$1" = shell ] && [ -n "$FAKE_SHELL_FAILS" ] && exit 1
exit 0
'''

gi = pytest.importorskip("gi")
from gi.repository import GLib  # noqa: E402


class Proxy:
    """A Gio.DBusProxy stand-in: cached properties and its g-properties-changed handler, held
    weakly: like a real proxy, it does not keep its owner alive."""

    def __init__(self, path, props):
        self.path = path
        self.props = dict(props)
        self.handler = None

    def connect(self, signal, handler):
        assert signal == "g-properties-changed"
        self.handler = weakref.WeakMethod(handler)

    def get_cached_property(self, name):
        return self.props.get(name)

    def change(self, **props):
        self.props.update(props)
        handler = self.handler()
        if handler:
            handler(self, GLib.Variant("a{sv}", props), [])


@pytest.fixture
def screen(fake_bin, monkeypatch):
    mod = load_script(SCRIPT)
    fake_bin.stub("waydroid", WAYDROID)
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    monkeypatch.setenv("FAKE_STATE", "RUNNING")
    proxies = {}

    def new_sync(bus, flags, info, name, path, iface, cancel):
        assert name == "org.freedesktop.login1"
        return proxies[path]

    monkeypatch.setattr(mod.Gio.DBusProxy, "new_sync", new_sync)
    mod.proxies = proxies
    return mod


def seat(mod, session="/org/freedesktop/login1/session/_31"):
    mod.proxies[mod.SEAT] = Proxy(mod.SEAT, {"ActiveSession": GLib.Variant("(so)", ("1", session))})
    return mod.proxies[mod.SEAT]


def session(mod, locked, path="/org/freedesktop/login1/session/_31"):
    mod.proxies[path] = Proxy(path, {"LockedHint": GLib.Variant("b", locked)})
    return mod.proxies[path]


def keyevents(fake_bin):
    return [c[-1] for c in fake_bin.calls("waydroid") if c[0] == "shell"]


def test_lock_and_unlock(screen, fake_bin, capsys):
    seat(screen)
    s = session(screen, False)
    w = screen.Watcher(None)  # noqa: F841 (held: its handlers live with it)
    # Unlocked at start: an Android that runs is awake already
    assert keyevents(fake_bin) == []
    s.change(LockedHint=GLib.Variant("b", True))
    s.change(LockedHint=GLib.Variant("b", True))
    s.change(LockedHint=GLib.Variant("b", False))
    assert keyevents(fake_bin) == ["KEYCODE_SLEEP", "KEYCODE_WAKEUP"]
    assert ["shell", "--", "input", "keyevent", "KEYCODE_SLEEP"] in fake_bin.calls("waydroid")
    out = capsys.readouterr().out
    assert "Phone locked: Android asleep" in out and "Phone unlocked: Android awake" in out


def test_locked_at_start_puts_android_to_sleep(screen, fake_bin):
    seat(screen)
    session(screen, True)
    w = screen.Watcher(None)  # noqa: F841 (held: its handlers live with it)
    assert keyevents(fake_bin) == ["KEYCODE_SLEEP"]


def test_unlock_thaws_a_frozen_container_first(screen, fake_bin, monkeypatch):
    seat(screen)
    s = session(screen, True)
    w = screen.Watcher(None)  # noqa: F841 (held: its handlers live with it)
    monkeypatch.setenv("FAKE_STATE", "FROZEN")
    s.change(LockedHint=GLib.Variant("b", False))
    calls = fake_bin.calls("waydroid")
    assert calls[-2:] == [["container", "unfreeze"], ["shell", "--", "input", "keyevent", "KEYCODE_WAKEUP"]]


def test_lock_of_a_frozen_container_does_not_thaw_it(screen, fake_bin, monkeypatch):
    monkeypatch.setenv("FAKE_STATE", "FROZEN")
    assert screen.apply(True)
    assert ["container", "unfreeze"] not in fake_bin.calls("waydroid")


@pytest.mark.parametrize("env", [{"FAKE_STATE": "STOPPED"}, {"FAKE_STATUS_FAILS": "1", "FAKE_STATE": ""}])
def test_without_android_nothing_happens(screen, fake_bin, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert screen.apply(True) is False
    assert keyevents(fake_bin) == []


def test_failed_keyevent_is_reported(screen, fake_bin, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_SHELL_FAILS", "1")
    assert screen.apply(True) is False
    assert "(failed)" in capsys.readouterr().out


def test_waydroid_missing(screen, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert screen.container_state() is None
    assert screen.run(["waydroid", "shell"]) is False


def test_waydroid_hanging(screen, monkeypatch):
    import subprocess

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired("waydroid", 20)
    monkeypatch.setattr(screen.subprocess, "run", hang)
    assert screen.container_state() is None
    assert screen.run(["waydroid", "shell"]) is False


def test_another_session_becomes_active(screen, fake_bin):
    st = seat(screen)
    session(screen, False)
    w = screen.Watcher(None)  # noqa: F841 (held: its handlers live with it)
    other = session(screen, True, "/org/freedesktop/login1/session/_32")
    st.change(ActiveSession=GLib.Variant("(so)", ("2", other.path)))
    assert keyevents(fake_bin) == ["KEYCODE_SLEEP"]
    # A change of another property of the seat or the session changes nothing
    st.change(CanGraphical=GLib.Variant("b", True))
    other.change(Active=GLib.Variant("b", True))
    assert keyevents(fake_bin) == ["KEYCODE_SLEEP"]


def test_no_active_session(screen, fake_bin):
    st = seat(screen, "/")
    w = screen.Watcher(None)
    assert w.session is None
    st.props["ActiveSession"] = None
    w.follow_session()
    assert w.session is None and keyevents(fake_bin) == []


def test_session_without_locked_hint(screen, fake_bin):
    seat(screen)
    screen.proxies["/org/freedesktop/login1/session/_31"] = Proxy("/org/freedesktop/login1/session/_31", {})
    w = screen.Watcher(None)
    assert w.locked is False and keyevents(fake_bin) == []


def test_main_without_system_bus(screen, monkeypatch, capsys):
    def no_bus(kind, cancel):
        raise GLib.Error("no bus")
    monkeypatch.setattr(screen.Gio, "bus_get_sync", no_bus)
    assert screen.main() == 1
    assert "Cannot watch the lock: no bus" in capsys.readouterr().err


def test_main_keeps_the_watcher_while_the_loop_runs(screen, fake_bin, monkeypatch):
    """The lock is still followed inside the loop: the watcher (and its proxies' handlers) must
    outlive the call that made it."""
    import gc
    seat(screen)
    s = session(screen, False)
    monkeypatch.setattr(screen.Gio, "bus_get_sync", lambda kind, cancel: None)

    def loop_run(self):
        gc.collect()
        s.change(LockedHint=GLib.Variant("b", True))
    monkeypatch.setattr(screen.GLib, "MainLoop", lambda: type("L", (), {"run": loop_run})())
    assert screen.main() == 0
    assert keyevents(fake_bin) == ["KEYCODE_SLEEP"]
