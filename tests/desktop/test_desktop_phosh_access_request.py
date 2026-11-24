# phosh-plinius 0111: an access dialog whose request goes away closes, so the next question can
# show. access-request.c of the patch exports org.freedesktop.impl.portal.Request at the
# request's handle and watches the caller; Close (as xdg-desktop-portal calls it when the app
# closes its request) or the caller leaving the bus (a direct caller such as
# plinius-appaccess's agent) ends the request, answered with response 2. The file is taken
# from the patch as it is and compiled into c/phosh_access_request_harness.c, which keeps the
# pending calls on a private session bus as the portal access manager does.

import threading
import time
from pathlib import Path

import pytest

from conftest import PKG, REPO, compile_c, load_script

PHOSH = PKG / "phosh-plinius"
PATCH = next(PHOSH.glob("0111-portal-access-manager-*.patch"))
HARNESS = Path(__file__).parent / "c/phosh_access_request_harness.c"
DHW = REPO / "tests/daemons-hw/dhw_daemon_bus.py"

HANDLE = "/org/freedesktop/portal/desktop/request/1_42/t1"
CANCELLED = (2, {"plinius-choice": "cancel"})


def new_file(patch, name):
    """A file the patch adds, as it is after the patch"""
    part = patch.read_text().split(f"+++ b/{name}\n", 1)[1].split("\ndiff --git", 1)[0]
    lines = part.splitlines()[1:]  # the hunk header
    assert all(line.startswith("+") for line in lines), name
    return "\n".join(line[1:] for line in lines) + "\n"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("access-request")
    for name in ("access-request.c", "access-request.h"):
        (d / name).write_text(new_file(PATCH, f"src/{name}"))
    return compile_c(d / "harness", [HARNESS],
                     flags=[f"-I{d}", f'-DACCESS_REQUEST_SOURCE="{d / "access-request.c"}"'],
                     pkgs=["gio-unix-2.0"])


@pytest.fixture
def dhw():
    pytest.importorskip("gi")
    return load_script(DHW, "desk_dhw_daemon_bus")


class Asker:
    """A caller of AccessDialog on a connection of its own; the call blocks in a thread"""

    def __init__(self, bus, handle):
        self.conn = bus.connect()
        self.result = self.error = None
        self.thread = threading.Thread(target=self._run, args=(handle,), daemon=True)
        self.thread.start()

    def _run(self, handle):
        from gi.repository import Gio, GLib
        try:
            r = self.conn.call_sync("org.plinius.Test", "/test", "org.plinius.Test", "Ask",
                                    GLib.Variant("(s)", (handle,)), GLib.VariantType("(ua{sv})"),
                                    Gio.DBusCallFlags.NONE, 20000, None)
            self.result = r.unpack()
        except GLib.Error as e:
            self.error = e

    def answer(self):
        self.thread.join(20)
        assert not self.thread.is_alive(), "the call was never answered"
        return self.result

    def leave(self):
        """The caller leaves the bus"""
        self.conn.close_sync(None)
        self.thread.join(20)


class World:
    def __init__(self, dhw, harness, tmp_path):
        self.dhw = dhw
        tmp_path.mkdir(parents=True)
        self.bus = dhw.PrivateBus(tmp_path)
        env = dhw.sanitized_env(DBUS_SESSION_BUS_ADDRESS=self.bus.address, G_MESSAGES_DEBUG="all",
                                G_DEBUG="fatal-criticals")
        self.harness = dhw.Daemon([harness], env)
        dhw.poll(lambda: "ready\n" in self.harness.out, what="the harness")
        self.askers = []

    def ask(self, handle=HANDLE):
        want = self.pending() + 1
        a = Asker(self.bus, handle)
        self.askers.append(a)
        self.dhw.poll(lambda: self.pending() == want, what=f"{want} pending")
        return a

    def pending(self):
        return self.bus.call("org.plinius.Test", "/test", "org.plinius.Test", "Pending", None, "(u)")[0]

    def answer(self, handle=HANDLE):
        self.bus.call("org.plinius.Test", "/test", "org.plinius.Test", "Answer",
                      self.dhw_variant("(s)", (handle,)))

    def dhw_variant(self, fmt, value):
        from gi.repository import GLib
        return GLib.Variant(fmt, value)

    def close_request(self, handle=HANDLE):
        """Request.Close as xdg-desktop-portal calls it on Phosh's request object"""
        return self.bus.call("org.plinius.Test", handle, "org.freedesktop.impl.portal.Request", "Close")

    def gone(self):
        return [line.rstrip("\n").split(" ", 2)[1:] for line in self.harness.out if line.startswith("gone ")]

    def close(self):
        code, stderr, stdout = self.harness.stop()
        for a in self.askers:
            if a.thread.is_alive():
                a.leave()
        self.bus.close()
        assert code == 0 and "exit" in stdout, stderr
        return stderr


@pytest.fixture
def world(dhw, harness, tmp_path):
    worlds = []

    def make():
        w = World(dhw, harness, tmp_path / str(len(worlds)))
        worlds.append(w)
        return w

    yield make
    for w in worlds:
        w.stderr = w.close()


def unknown(excinfo):
    msg = str(excinfo.value)
    return "UnknownMethod" in msg or "UnknownObject" in msg


def test_close_ends_the_request_as_cancelled(world):
    w = world()
    a = w.ask()
    assert w.close_request() == ()
    assert a.answer() == CANCELLED
    assert w.gone() == [["closed", HANDLE]]
    assert w.pending() == 0
    # The object is gone with the request: a second Close finds nothing
    from gi.repository import GLib
    with pytest.raises(GLib.Error) as e:
        w.close_request()
    assert unknown(e)
    assert w.gone() == [["closed", HANDLE]]


def test_a_caller_leaving_the_bus_ends_the_request(world):
    w = world()
    a = w.ask()
    a.leave()
    w.dhw.poll(lambda: w.gone(), what="the request gone")
    assert w.gone() == [["vanished", HANDLE]]
    assert w.pending() == 0
    assert a.error is not None and a.result is None


def test_an_answered_request_is_never_gone(world):
    from gi.repository import GLib
    w = world()
    a = w.ask()
    w.answer()
    assert a.answer() == (0, {})
    with pytest.raises(GLib.Error) as e:
        w.close_request()
    assert unknown(e)
    a.leave()
    time.sleep(0.3)
    w.ask(handle="/other")      # something still to wait for on the bus
    w.close_request("/other")
    w.dhw.poll(lambda: w.gone(), what="the other request gone")
    assert w.gone() == [["closed", "/other"]]


def test_each_request_goes_on_its_own(world):
    w = world()
    first = w.ask(handle="/r/1")
    second = w.ask(handle="/r/2")
    w.close_request("/r/2")
    assert second.answer() == CANCELLED
    assert w.pending() == 1
    first.leave()
    w.dhw.poll(lambda: w.pending() == 0, what="no request open")
    assert w.gone() == [["closed", "/r/2"], ["vanished", "/r/1"]]


def test_a_request_goes_only_once(world):
    # Closed and then its caller gone, before the shell freed it: one end only
    w = world()
    a = w.ask(handle="/r/keep")
    w.close_request("/r/keep")
    assert a.answer() == CANCELLED
    a.leave()
    time.sleep(0.3)
    assert w.gone() == [["closed", "/r/keep"]]
    w.answer("/r/keep")
    assert w.pending() == 0


def test_a_handle_that_is_no_object_path_still_watches_the_caller(world):
    w = world()
    a = w.ask(handle="not a path")
    w.harness.wait_for("Request handle 'not a path' is no object path")
    a.leave()
    w.dhw.poll(lambda: w.gone(), what="the request gone")
    assert w.gone() == [["vanished", "not a path"]]


def test_a_handle_exported_already_still_watches_the_caller(world):
    w = world()
    first = w.ask()
    second = w.ask()
    w.harness.wait_for(f"Could not export request {HANDLE}: ")
    # Close reaches the first request's object only
    w.close_request()
    assert first.answer() == CANCELLED
    assert w.pending() == 1
    second.leave()
    w.dhw.poll(lambda: w.pending() == 0, what="no request open")
    assert w.gone() == [["closed", HANDLE], ["vanished", HANDLE]]


def test_open_requests_are_freed_with_the_shell(world):
    w = world()
    w.ask()
    w.ask(handle="/r/2")
    # The world's close stops the harness with both open: it must still exit cleanly
