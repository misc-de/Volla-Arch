# Fakes shared by the tests of the UI programs (tests/desktop).
#
# GTK 3 and GTK 4 cannot both be loaded into one process, and the whole test run is one process.
# So the toolkit namespaces (Gtk, Gdk, Adw, GtkLayerShell, Gsk, the Evolution libraries) are
# replaced by permissive stand-ins while a program is imported; GLib, Gio, Pango, Graphene and
# cairo stay real. Nothing here opens a window or talks to a bus.

import sys
import types

import gi
import pytest
from gi.repository import GLib

from conftest import load_script

TOOLKITS = ("Gtk", "Gdk", "Adw", "GtkLayerShell", "Gsk")


class Stub:
    """Takes any use: attributes and calls give more stubs, so widget code runs without GTK."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return Stub()

    def __call__(self, *args, **kwargs):
        return Stub()

    def __iter__(self):
        return iter(())


class _Meta(type):
    """Class attributes of a fake namespace are classes again (Gtk.Window, Gtk.Align.END ...),
    created on first use and then the same object, so comparisons by identity work."""

    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        sub = _Meta(name, (Stub,), {})
        setattr(cls, name, sub)
        return sub


def namespace(name):
    return _Meta(name, (Stub,), {})


def load_gui(path, name, fakes=TOOLKITS, **extra):
    """Import a program with the toolkit namespaces faked; extra=namespace objects to use for
    other names (e.g. EBook=...). The module keeps its fakes after the import."""
    names = dict({n: namespace(n) for n in fakes}, **extra)
    with pytest.MonkeyPatch.context() as mp:
        real = gi.require_version
        # The real namespaces still get their version (no "imported without a version" warning)
        mp.setattr(gi, "require_version",
                   lambda ns, version: None if ns in names else real(ns, version))
        repository = sys.modules["gi.repository"]
        for n, obj in names.items():
            mp.setattr(repository, n, obj, raising=False)
            mp.setitem(sys.modules, "gi.repository." + n, obj)
        return load_script(path, name)


class Proxy:
    """A module with some attributes replaced, everything else from the real one."""

    def __init__(self, real, **overrides):
        self._real = real
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._real, name)


class FakeGLib(Proxy):
    """GLib with a hand-driven clock and main loop sources: nothing waits, nothing runs by
    itself. fire(id) runs a source like the main loop would."""

    def __init__(self, **overrides):
        super().__init__(GLib, **overrides)
        self.sources = {}
        self.next_id = 1
        self.now_us = 1_000_000_000
        self.removed = []
        self.slept = []

    def _add(self, kind, interval, func, args):
        sid = self.next_id
        self.next_id += 1
        self.sources[sid] = (kind, interval, func, args)
        return sid

    def timeout_add(self, interval, func, *args, **_kw):
        return self._add("timeout", interval, func, args)

    def timeout_add_seconds(self, interval, func, *args, **_kw):
        return self._add("seconds", interval, func, args)

    def idle_add(self, func, *args, **_kw):
        return self._add("idle", 0, func, args)

    def child_watch_add(self, _priority, pid, func, *args):
        return self._add("child", pid, func, args)

    def source_remove(self, sid):
        self.removed.append(sid)
        return self.sources.pop(sid, None) is not None

    def get_monotonic_time(self):
        return self.now_us

    def usleep(self, us):
        self.slept.append(us)

    def advance_ms(self, ms):
        self.now_us += int(ms * 1000)

    def of_kind(self, kind):
        return [sid for sid, s in self.sources.items() if s[0] == kind]

    def fire(self, sid, *extra):
        kind, interval, func, args = self.sources[sid]
        keep = func(*extra, *args)
        if not keep and sid in self.sources:
            del self.sources[sid]
        return keep


class Invocation:
    """A D-Bus method invocation that records its answer."""

    def __init__(self):
        self.value = self.error = None
        self.answered = 0

    def return_value(self, value):
        self.value = value
        self.answered += 1

    def return_dbus_error(self, name, message):
        self.error = (name, message)
        self.answered += 1


def gerror(message="failed"):
    return GLib.Error(message)


def recorder():
    """A callable that records its calls: rec.calls is a list of argument tuples."""
    calls = []

    def rec(*args, **kwargs):
        calls.append(args)
    rec.calls = calls
    return rec


def as_module(**attrs):
    return types.SimpleNamespace(**attrs)
