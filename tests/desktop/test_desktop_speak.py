# plinius-speak: the text from the arguments or stdin, waiting for its Finished (also one that
# comes before Speak returns), the exit status (said, stopped, no voice) and SIGTERM stopping the
# speech, as speech-dispatcher's generic module does. The session bus and the main loop are faked.

import io

import pytest
from gi.repository import Gio, GLib

import desktop_fakes as fakes
from conftest import PKG, load_script

SPEAK = PKG / "plinius-speech" / "plinius-speak"


@pytest.fixture(scope="module")
def mod():
    return load_script(SPEAK, "plinius_speak_under_test")


class Loop:
    def __init__(self):
        self.on_run = None
        self.quits = 0
        self.runs = 0

    def run(self):
        self.runs += 1
        if self.on_run:
            self.on_run()

    def quit(self):
        self.quits += 1


class Bus:
    def __init__(self, error=None, early=None):
        self.calls = []
        self.handler = None
        self.error = error
        self.early = early          # (id, completed) delivered while Speak runs

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, cb):
        assert member == "Finished"
        self.handler = cb

    def finished(self, sid, completed):
        self.handler(self, ":1.5", "/org/plinius/Speech1", "org.plinius.Speech1", "Finished",
                     GLib.Variant("(ub)", (sid, completed)))

    def call_sync(self, dest, path, iface, method, params, *rest):
        self.calls.append((method, params.unpack()))
        if self.error and method in self.error:
            raise self.error[method]
        if method == "Speak":
            if self.early:
                self.finished(*self.early)
            return GLib.Variant("(u)", (7,))
        return None


def no_voice():
    return GLib.Error.new_literal(Gio.DBusError.quark(), "GDBus.Error:org.plinius.Speech1.Error.NoVoice: none",
                                  Gio.DBusError.FAILED)


def test_read_text(mod):
    assert mod.read_text(["x", "Hallo", "Welt"], io.StringIO("")) == "Hallo Welt"
    assert mod.read_text(["x", "--stdin"], io.StringIO("Zeile\n")) == "Zeile\n"


@pytest.mark.parametrize("completed,status", [(True, 0), (False, 1)])
def test_waits_for_its_finished(mod, completed, status):
    bus, loop = Bus(), Loop()
    s = mod.Speaker(bus, loop)
    loop.on_run = lambda: (bus.finished(3, True), bus.finished(7, completed))  # another id first
    assert s.speak("Hallo") == status
    assert bus.calls == [("Speak", ("Hallo", {}))] and loop.quits == 1


def test_finished_before_speak_returned(mod):
    bus, loop = Bus(early=(7, True)), Loop()
    assert mod.Speaker(bus, loop).speak("Hallo") == 0
    assert loop.runs == 0


def test_errors(mod, capsys):
    assert mod.Speaker(Bus(error={"Speak": no_voice()}), Loop()).speak("x") == 2
    assert mod.Speaker(Bus(error={"Speak": GLib.Error("no bus")}), Loop()).speak("x") == 1
    assert "no bus" in capsys.readouterr().err


def test_sigterm_stops_the_speech(mod, capsys):
    bus, loop = Bus(), Loop()
    s = mod.Speaker(bus, loop)
    assert s.stop() == GLib.SOURCE_REMOVE          # before Speak: nothing to stop
    assert bus.calls == []
    loop.on_run = s.stop
    assert s.speak("x") == 1
    assert bus.calls[-1] == ("Stop", (7,))
    bus.error = {"Stop": GLib.Error("gone")}
    s.stop()
    assert "gone" in capsys.readouterr().err


def test_main(mod, monkeypatch):
    assert mod.main(["plinius-speak"], io.StringIO("")) == 0          # nothing to say
    bus = Bus()
    signals = []
    loop = Loop()
    loop.on_run = lambda: bus.finished(7, True)
    glib = fakes.FakeGLib(MainLoop=lambda: loop,
                          unix_signal_add=lambda prio, sig, cb: signals.append(sig))
    monkeypatch.setattr(mod, "GLib", glib)
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(Gio, bus_get_sync=lambda t, c: bus))
    assert mod.main(["plinius-speak", "--stdin"], io.StringIO(" Guten Tag \n")) == 0
    assert bus.calls == [("Speak", ("Guten Tag", {}))]
    assert len(signals) == 2


def test_runs_as_a_program():
    from desktop_mainrun import run_as_main
    # Without a text there is nothing to say: no bus, exit 0
    assert run_as_main(SPEAK) == 0
