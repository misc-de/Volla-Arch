# plinius-usb-prompt: the notification in the session asking which USB port stays active when a
# pogo accessory and a USB-C device meet, what an answer does (ChoosePort, the note that the
# switch takes a moment) and the note when the accessory alone takes the port. It needs no
# display: the question is a notification. Command mode fakes the two buses
# (usb/dhw_usb_prompt_harness.c); main() runs against a fake plinius-usb and notification server
# on a private bus (dhw_usb_fakeservice.h).

import os
import time
from pathlib import Path

import pytest

from dhw_harness import build_harness, run, sandboxed_source
from dhw_usb_phone import USB, Daemon, PrivateBus, Result

HERE = Path(__file__).resolve().parent
SRC = USB / "plinius-usb-prompt.c"
NOTIFY = "notify org.freedesktop.Notifications Notify"
BODY = ("A pogo accessory and a USB-C device are connected, only one of them can carry data. "
        "Without an answer, {} stays active.")
QUESTION = (NOTIFY + " replaces={} title=Which port should be active? body=" + BODY
            + " actions=usb-c,USB-C,pogo,Pogo urgency=2 transient=1 category=device expire=8000 timeout=2000")
ACCESSORY = (NOTIFY + " replaces=0 title=Pogo accessory active body=USB-C carries no data until the "
             "accessory is removed. actions= urgency=1 transient=1 category=device expire=5000 timeout=2000")
SWITCHING = (NOTIFY + " replaces=0 title=Switching to {} body=This can take up to 10 seconds. It stays that "
             "way until the accessory or the cable is removed. actions= urgency=1 transient=1 category=device "
             "expire=5000 timeout=2000")
CLOSE = "call org.freedesktop.Notifications CloseNotification ({},)"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("usb-prompt")
    sandboxed_source(SRC, d, [])
    code = (HERE / "usb" / "dhw_usb_prompt_harness.c").read_text()
    return build_harness(d, "prompt", code, flags=[f"-I{HERE / 'usb'}", "-Wno-unused-parameter"],
                         pkgs=("gio-unix-2.0",))


@pytest.fixture
def prompt(built, tmp_path):
    def go(*commands):
        env = dict(os.environ)
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        return Result(run(built, *commands, env=env))
    return go


# ---- The question ----

@pytest.mark.parametrize("stays,name", [("usb-c", "USB-C"), ("pogo", "Pogo")])
def test_the_question(prompt, stays, name):
    r = prompt("prop ActivePort usb-c", f"prop PortQuestion {stays}", "start", "state")
    assert r.of("start") == [QUESTION.format(0, name)]
    assert r.of("state") == ["state question=10 active=usb-c"]


def test_one_question_at_a_time(prompt):
    r = prompt("prop PortQuestion usb-c", "start", "update", "prop PortQuestion pogo", "update")
    assert r.of("update") == []
    # Still the one shown: plinius-usb's PortQuestion only changes with a new conflict
    assert r.of("update", 1) == []


def test_the_question_closes_when_plinius_usb_says_so(prompt):
    r = prompt("prop PortQuestion usb-c", "start", "prop PortQuestion -", "update", "state", "update")
    assert r.of("update") == [CLOSE.format(10)]
    assert r.of("state") == ["state question=0 active="]
    assert r.of("update", 1) == []


def test_a_notification_server_that_fails(prompt):
    r = prompt("prop PortQuestion usb-c", "notify-fails", "start", "state", "update")
    assert "Cannot show the notification: fake bus: no notification server" in r.stderr
    assert r.of("state") == ["state question=0 active="]
    # Asked again at the next change
    assert r.of("update") == [QUESTION.format(0, "USB-C")]


def test_without_plinius_usb_nothing_is_asked(prompt):
    r = prompt("start", "state")
    assert r.of("start") == []
    assert r.of("state") == ["state question=0 active="]


# ---- The answer ----

def test_answering_switches(prompt):
    r = prompt("prop PortQuestion usb-c", "start", "signal ActionInvoked 10 pogo", "state", "chosen ok")
    # The driver takes the choice when its 10 seconds are over: a note says so
    assert r.of("signal ActionInvoked 10 pogo") == ["proxy ChoosePort ('pogo',)", SWITCHING.format("Pogo")]
    assert r.of("state") == ["state question=0 active="]
    assert r.of("chosen ok") == []
    assert "did not count" not in r.stderr


def test_switching_to_usb_c(prompt):
    r = prompt("prop PortQuestion pogo", "start", "signal ActionInvoked 10 usb-c")
    assert r.of("signal ActionInvoked 10 usb-c") == ["proxy ChoosePort ('usb-c',)", SWITCHING.format("USB-C")]


def test_keeping_what_stays_needs_no_note(prompt):
    r = prompt("prop PortQuestion usb-c", "start", "signal ActionInvoked 10 usb-c")
    assert r.of("signal ActionInvoked 10 usb-c") == ["proxy ChoosePort ('usb-c',)"]


def test_an_answer_after_the_question_went(prompt):
    r = prompt("prop PortQuestion usb-c", "start", "prop PortQuestion -", "signal ActionInvoked 10 pogo")
    assert r.of("signal ActionInvoked 10 pogo") == ["proxy ChoosePort ('pogo',)"]


def test_an_answer_that_did_not_count(prompt):
    # Locked meanwhile or too late: nothing changes, as without an answer
    r = prompt("prop PortQuestion usb-c", "start", "signal ActionInvoked 10 pogo", "chosen error")
    assert "The choice did not count: Unlock the phone first" in r.stderr


@pytest.mark.parametrize("signal", ["signal ActionInvoked 10 default", "signal ActionInvoked 10 usb-c;x"])
def test_only_the_two_buttons_count(prompt, signal):
    # Tapping the notification itself is no answer
    r = prompt("prop PortQuestion usb-c", "start", signal, "state")
    assert r.of(signal) == []
    assert r.of("state") == ["state question=0 active="]


def test_signals_of_other_notifications(prompt):
    r = prompt("prop PortQuestion usb-c", "start", "signal ActionInvoked 7 pogo", "signal NotificationClosed 7",
               "state", "signal NotificationClosed 10", "state", "close")
    assert r.of("signal ActionInvoked 7 pogo") == []
    assert r.of("state") == ["state question=10 active="]
    # Closed by the user or the server: nothing left to close
    assert r.of("state", 1) == ["state question=0 active="]
    assert r.of("close") == []


# ---- The accessory alone ----

def test_the_accessory_alone_gets_a_note(prompt):
    r = prompt("prop ActivePort none", "start", "prop ActivePort pogo", "update", "update")
    assert r.of("update") == [ACCESSORY]
    assert r.of("update", 1) == []


@pytest.mark.parametrize("before", ["usb-c", "pogo"])
def test_no_note_when_pogo_was_chosen(prompt, before):
    # From USB-C: the user answered the question; at the start: nothing changed
    r = prompt(f"prop ActivePort {before}", "start", "prop ActivePort pogo", "update")
    assert r.of("start") == []
    assert r.of("update") == []


# ---- main() on a private bus ----

@pytest.fixture
def bus(tmp_path):
    b = PrivateBus(tmp_path)
    yield b
    b.close()


def program(built, address, *properties, session_bus=None):
    class Program:
        exe = built

        @staticmethod
        def env():
            return dict(os.environ)
    return Daemon(Program, address, args=("daemon", *(properties or ["-"])), session_bus=session_bus)


def service(line):
    return line.startswith("service ")


def test_main_asks_and_answers(built, bus):
    d = program(built, bus.address, "PortQuestion=usb-c", "ActivePort=usb-c")
    try:
        assert d.wait_line(service) == (
            "service Notify 1 app=plinius-usb icon=plinius-usb-symbolic replaces=0 title=Which port should be "
            f"active? body={BODY.format('USB-C')} actions=usb-c,USB-C,pogo,Pogo urgency=2 transient=1 "
            "category=device expire=8000")
        d.command("svc signal ActionInvoked 1 pogo")
        assert d.wait_line(service) == "service ChoosePort pogo"
        assert d.wait_line(service).startswith("service Notify 2 app=plinius-usb icon=plinius-usb-symbolic "
                                               "replaces=0 title=Switching to Pogo")
        # A new question, closed when plinius-usb says so
        d.command("svc set PortQuestion pogo")
        assert d.wait_line(service).startswith("service Notify 3 ")
        d.command("svc set PortQuestion -")
        assert d.wait_line(service) == "service Close 3"
        # The accessory alone
        d.command("svc set ActivePort none")
        d.command("svc set ActivePort pogo")
        assert "title=Pogo accessory active" in d.wait_line(service)
        # An answer that does not count
        d.command("svc set PortQuestion usb-c")
        assert d.wait_line(service).startswith("service Notify 5 ")
        d.command("svc choose-fails")
        d.command("svc signal NotificationClosed 9")
        d.command("svc signal ActionInvoked 5 pogo")
        assert d.wait_line(service) == "service ChoosePort pogo"
        end = time.monotonic() + 10 * d.slow
        while "The choice did not count" not in d.stderr:
            assert time.monotonic() < end, d.stderr
            time.sleep(0.02)
        assert ("The choice did not count: GDBus.Error:org.freedesktop.DBus.Error.Failed: "
                "There is no question open") in d.stderr
        # A question still open when the session ends is closed
        d.command("svc set PortQuestion -")
        d.command("svc set PortQuestion pogo")
        line = d.wait_line(lambda line: line.startswith("service Notify"))
        n = line.split()[2]
        d.command("quit")
        assert d.wait_line(service) == f"service Close {n}"
        assert d.finish() == 0
    finally:
        d.kill()


def test_main_follows_plinius_usb_restarting(built, bus):
    d = program(built, bus.address, "PortQuestion=usb-c")
    try:
        assert d.wait_line(service).startswith("service Notify 1 ")
        # plinius-usb goes: its question goes with it
        d.command("svc release")
        assert d.wait_line(service) == "service Close 1"
        d.command("svc own")
        assert d.wait_line(service).startswith("service Notify 2 ")
        d.command("svc signal NotificationClosed 2")
        d.command("quit")
        assert d.finish() == 0
        assert "service Close 2" not in d.lines
    finally:
        d.kill()


def test_main_without_a_session_bus(built, bus, tmp_path):
    d = program(built, bus.address, session_bus=f"unix:path={tmp_path}/no-bus")
    try:
        assert d.finish() == 1
        assert "No session bus" in d.stderr
    finally:
        d.kill()


def test_main_without_a_system_bus(built, bus, tmp_path):
    d = program(built, f"unix:path={tmp_path}/no-bus", session_bus=bus.address)
    try:
        assert d.finish() == 1
        assert "No connection to plinius-usb" in d.stderr
    finally:
        d.kill()
