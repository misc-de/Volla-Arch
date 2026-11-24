# Helpers for running a daemon's real main() in the tests of plinius-charge, plinius-torch,
# plinius-energy and vibrator-ff-bridge: a private dbus-daemon (socket in tmp, nothing
# activatable, never the host's buses), a fake service on it (a name, an object, signals),
# and a daemon process whose stderr is collected while it runs.

import queue
import shutil
import signal
import subprocess
import threading
import time
import uuid
import warnings
from pathlib import Path

import pytest

BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>system</type>
  <listen>{listen}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


class PrivateBus:
    """A dbus-daemon of the test alone"""

    def __init__(self, tmp):
        if shutil.which("dbus-daemon") is None:
            pytest.skip("no dbus-daemon")
        tmp = Path(tmp)
        sock = tmp / "bus"
        # A socket path has at most 107 bytes; a long tmp path gets an abstract socket instead
        listen = (f"unix:path={sock}" if len(str(sock)) < 100
                  else f"unix:abstract=dhw-daemon-{uuid.uuid4().hex}")
        conf = tmp / "bus.conf"
        conf.write_text(BUS_CONFIG.format(listen=listen))
        self.proc = subprocess.Popen(["dbus-daemon", f"--config-file={conf}", "--nofork", "--print-address"],
                                     stdout=subprocess.PIPE, text=True,
                                     env={"PATH": "/usr/bin:/bin", "HOME": str(tmp)})
        self.address = self.proc.stdout.readline().strip()
        assert self.address.startswith("unix:"), "dbus-daemon gave no address"

    def connect(self):
        from gi.repository import Gio
        flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                 | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
        return Gio.DBusConnection.new_for_address_sync(self.address, flags, None, None)

    def call(self, dest, path, iface, method, params=None, reply="()", timeout=5000):
        """A method call from a client of the test; returns the unpacked reply"""
        from gi.repository import Gio, GLib
        c = self.connect()
        try:
            r = c.call_sync(dest, path, iface, method, params, GLib.VariantType(reply),
                            Gio.DBusCallFlags.NO_AUTO_START, timeout, None)
            return r.unpack()
        finally:
            c.close_sync(None)

    def wait_name(self, name, timeout=20):
        from gi.repository import GLib
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            owned = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                              "NameHasOwner", GLib.Variant("(s)", (name,)), "(b)")[0]
            if owned:
                return
            time.sleep(0.02)
        raise AssertionError(f"{name} never appeared on the bus")

    def close(self):
        self.proc.send_signal(signal.SIGTERM)
        self.proc.wait(timeout=10)


class FakeService:
    """A service on the private bus, in a thread of its own: owns `name`, answers the methods of
    `xml` at `path` from `methods` (name -> function(params) -> GLib.Variant or None), records the
    calls, and emits signals on request."""

    def __init__(self, bus, name, path, xml, methods):
        from gi.repository import Gio, GLib
        self.calls = []
        self.ctx = GLib.MainContext.new()
        self.loop = GLib.MainLoop.new(self.ctx, False)
        ready = threading.Event()

        def thread():
            self.ctx.push_thread_default()
            self.conn = bus.connect()
            info = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]

            def on_call(conn, sender, obj, iface, method, params, invocation):
                self.calls.append((method, params.unpack()))
                invocation.return_value(methods[method](params))

            with warnings.catch_warnings():
                # PyGObject prefers register_object_with_closures2, which older ones lack
                warnings.simplefilter("ignore", DeprecationWarning)
                self.conn.register_object(path, info, on_call, None, None)
            self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "RequestName", GLib.Variant("(su)", (name, 0)), GLib.VariantType("(u)"),
                                Gio.DBusCallFlags.NONE, 5000, None)
            ready.set()
            self.loop.run()
            self.ctx.pop_thread_default()

        self.thread = threading.Thread(target=thread, daemon=True)
        self.thread.start()
        assert ready.wait(10), f"{name} did not start"

    def emit(self, path, iface, signal_name, params):
        done = threading.Event()

        def emit():
            self.conn.emit_signal(None, path, iface, signal_name, params)
            self.conn.flush_sync(None)
            done.set()
            return False

        self.ctx.invoke_full(0, emit)
        assert done.wait(10)

    def close(self):
        self.ctx.invoke_full(0, lambda: (self.conn.close_sync(None), self.loop.quit(), False)[-1])
        self.thread.join(10)


class Daemon:
    """A daemon process; stderr lines are collected while it runs, so a test can wait for one"""

    def __init__(self, args, env, cwd=None):
        self.proc = subprocess.Popen([str(a) for a in args], env=env, cwd=cwd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        self.lines = queue.Queue()
        self.seen = []
        self.out = []
        threading.Thread(target=self._pump, args=(self.proc.stderr, self.lines), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stdout, None), daemon=True).start()

    def _pump(self, stream, q):
        for line in stream:
            if q is None:
                self.out.append(line)
            else:
                q.put(line)

    def wait_for(self, text, timeout=20):
        end = time.monotonic() + timeout
        while True:
            for line in self.seen:
                if text in line:
                    return line
            left = end - time.monotonic()
            if left <= 0:
                raise AssertionError(f"{text!r} never came; stderr so far:\n{''.join(self.seen)}")
            try:
                self.seen.append(self.lines.get(timeout=left))
            except queue.Empty:
                pass

    def stop(self, sig=signal.SIGTERM, timeout=20):
        """Signal it, wait for the exit; returns (exit code, stderr, stdout)"""
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
        code = self.proc.wait(timeout=timeout)
        time.sleep(0.05)
        while not self.lines.empty():
            self.seen.append(self.lines.get())
        return code, "".join(self.seen), "".join(self.out)


def poll(predicate, timeout=20, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"{what} never became true")


def sanitized_env(**extra):
    """The environment of a sanitized harness (the settings of dhw_harness.run), with the buses
    pointing at nothing unless given"""
    import os
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": "/nonexistent",
           "ASAN_OPTIONS": "detect_leaks=0:abort_on_error=0:exitcode=86",
           "UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=1:exitcode=87",
           "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/nonexistent/system-bus",
           "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/session-bus"}
    for k in ("GCOV_PREFIX", "GCOV_PREFIX_STRIP"):
        if k in os.environ:
            env[k] = os.environ[k]
    env.update({k: str(v) for k, v in extra.items()})
    return env


__all__ = ["PrivateBus", "FakeService", "Daemon", "poll", "sanitized_env"]
