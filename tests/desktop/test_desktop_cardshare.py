# plinius-cardshare: the vCard that goes out (chosen fields, the QR code's size limit), cards
# that come in over NFC and Bluetooth, and the session's state machine. The Evolution libraries
# are not on the build host: a small vCard stand-in takes their place. Buses are faked, files
# live in tmp_path.

import os
import signal as mod_signal
import subprocess as mod_subprocess
import types

import pytest
from gi.repository import Gio, GLib

import desktop_fakes as fakes
from conftest import PKG
from desktop_mainrun import run_as_main

CARDSHARE = PKG / "plinius-cardshare" / "plinius-cardshare.py"


# ----- a vCard stand-in for EBookContacts.VCard -----

class Param:
    def __init__(self, name, values):
        self.name, self.values = name, values

    def get_name(self):
        return self.name


class Attr:
    def __init__(self, name, values, params=()):
        self.name, self.values, self.params = name, list(values), list(params)

    @classmethod
    def parse(cls, line):
        head, value = line.split(":", 1)
        name, *params = head.split(";")
        return cls(name, value.split(";"),
                   [Param(*p.split("=", 1)) if "=" in p else Param("TYPE", p) for p in params])

    def get_name(self):
        return self.name

    def get_values(self):
        return list(self.values)

    def get_value(self):
        return self.values[0] if self.values else None

    def get_params(self):
        return list(self.params)

    def remove_param(self, name):
        self.params = [p for p in self.params if p.name.upper() != name.upper()]

    def copy(self):
        return Attr(self.name, self.values, self.params)

    def line(self):
        params = "".join(f";{p.name}={p.values}" for p in self.params)
        return f"{self.name}{params}:{';'.join(self.values)}"


class VCard:
    def __init__(self, attributes):
        self.attributes = attributes

    @classmethod
    def new_from_string(cls, text):
        lines = [l for l in text.replace("\r\n", "\n").split("\n") if l]
        if not lines or lines[0].upper() != "BEGIN:VCARD":
            raise GLib.Error("not a vCard")
        return cls([Attr.parse(l) for l in lines[1:]
                    if l.upper() not in ("END:VCARD",) and not l.upper().startswith("VERSION:")])

    def get_attribute(self, name):
        return next((a for a in self.attributes if a.name.upper() == name), None)

    def get_attributes(self):
        return list(self.attributes)

    def append_attribute(self, attr):
        self.attributes.append(attr)

    def to_string(self):
        return "".join(f"{l}\r\n" for l in ["BEGIN:VCARD", "VERSION:3.0"]
                       + [a.line() for a in self.attributes] + ["END:VCARD"])


class Contact(VCard):
    def __init__(self, attributes, props=None):
        super().__init__(attributes)
        self.props = dict(props or {})
        self.removed = []

    @classmethod
    def new_from_vcard(cls, text):
        return cls(VCard.new_from_string(text).attributes, {"id": "sender-id"})

    def get_property(self, name):
        return self.props.get(name)

    def set_property(self, name, value):
        self.props[name] = value

    def remove_attributes(self, group, name):
        self.removed.append(name)
        self.attributes = [a for a in self.attributes if a.name != name]


EBOOK_CONTACTS = types.SimpleNamespace(VCard=VCard, Contact=Contact)


@pytest.fixture(scope="module")
def mod():
    return fakes.load_gui(CARDSHARE, "plinius_cardshare_under_test",
                          fakes=("EBook", "EDataServer"), EBookContacts=EBOOK_CONTACTS)


def card(*lines):
    return "BEGIN:VCARD\r\nVERSION:3.0\r\n" + "".join(l + "\r\n" for l in lines) + "END:VCARD\r\n"


# ----- card_summary -----

@pytest.mark.parametrize("text, summary", [
    (card("FN:Anna Berg", "N:Berg;Anna;;;", "TEL;TYPE=CELL:+49 170 1234567",
          "EMAIL:anna@example.org"), ("Anna Berg", "+49 170 1234567")),
    (card("FN:", "N:Berg;Anna;;;", "EMAIL: anna@example.org "), ("Anna Berg", "anna@example.org")),
    (card("N:Berg;;;;"), ("Berg", "")),
    (card("N:;Anna;;;"), ("Anna", "")),
    (card("ORG:Firma"), ("Unnamed contact", "")),
])
def test_card_summary(mod, text, summary):
    assert mod.card_summary(text) == summary


def test_card_summary_without_family_name(mod):
    assert mod.card_summary(card("N:;John;Quincy;;"))[0] == "John"


# ----- the own card -----

class Settings:
    def __init__(self, fields=("name", "phone"), seconds=30, book="book", uid="me"):
        self.values = {"fields": list(fields), "seconds": seconds, "contact-book": book,
                       "contact-uid": uid}

    def get_strv(self, key):
        return self.values[key]

    def get_string(self, key):
        return self.values[key]

    def get_uint(self, key):
        return self.values[key]


OWN = [Attr("UID", ["x-1"]), Attr("FN", ["Anna Berg"]), Attr("N", ["Berg", "Anna", "", "", ""]),
       Attr("TEL", ["+491701234567"], [Param("TYPE", "CELL"), Param("X-EVOLUTION-UI-SLOT", "1")]),
       Attr("EMAIL", ["anna@example.org"]), Attr("ORG", ["Firma", "Team"]),
       Attr("TITLE", ["Boss"]), Attr("ADR", ["", "", "Weg 1", "Ort", "", "12345", "DE"]),
       Attr("URL", ["https://example.org"]), Attr("NOTE", ["n" * 800]),
       Attr("PHOTO", ["A" * 2000], [Param("ENCODING", "b")]), Attr("BDAY", ["1990-01-01"]),
       Attr("X-EVOLUTION-FILE-AS", ["Berg, Anna"])]


@pytest.fixture
def share(mod, tmp_path, monkeypatch):
    glib = fakes.FakeGLib(get_user_runtime_dir=lambda: str(tmp_path / "run"),
                          get_user_cache_dir=lambda: str(tmp_path / "cache"))
    (tmp_path / "run").mkdir()
    monkeypatch.setattr(mod, "GLib", glib)
    s = mod.CardShare.__new__(mod.CardShare)
    s.loop = types.SimpleNamespace(quit=lambda: None)
    s.settings = Settings()
    s.registry = None
    s.books = {}
    s.state, s.remaining, s.duration, s.card_text = "idle", 0, 0, ""
    s.channels, s.error, s.received, s.next_id = [], "", [], 1
    s.tick_id = s.idle_id = s.share_watch = s.tags_sub = s.agent_reg = 0
    s.inhibit_cookie = s.screensaver_sub = 0
    s.share_proc = s.workdir = s.nfc_was_enabled = None
    s.bt_was_discoverable = s.bt_timeout_before = None
    s.seen_tags, s.transfers, s.notifications = set(), {}, {}
    s.pushes = 0
    s.glib = glib
    s.signals = []
    s.changed = lambda *names: s.signals.append(names)
    s.own_contact = lambda: Contact([a.copy() for a in OWN], {"full-name": "Anna Berg"})
    s.events = []
    s.feedback = lambda event: s.events.append(("feedback", event))
    s.notify_received = lambda cid, name, detail: s.events.append(("notify", cid, name, detail))
    s.tmp = tmp_path
    return s


def names(text):
    return [line.split(":")[0].split(";")[0] for line in text.split("\r\n")
            if line and line not in ("BEGIN:VCARD", "VERSION:3.0", "END:VCARD")]


def test_own_card_has_only_the_chosen_fields(mod, share):
    text = mod.CardShare.own_card(share, False)
    assert text.startswith("BEGIN:VCARD\r\nVERSION:3.0\r\n")
    assert names(text) == ["FN", "N", "TEL"]
    # Evolution's own parameters stay at home, the others go along
    assert "TYPE=CELL" in text and "X-EVOLUTION" not in text


def test_own_card_always_has_the_name(mod, share):
    share.settings = Settings(fields=["email", "unknown-field"])
    assert names(mod.CardShare.own_card(share, False)) == ["FN", "N", "EMAIL"]


def test_qr_card_without_photo_and_small_enough(mod, share):
    share.settings = Settings(fields=list(mod.FIELD_PROPERTIES))
    full = mod.CardShare.own_card(share, False)
    assert "PHOTO" in names(full) and len(full.encode()) > mod.MAX_QR_BYTES
    qr = mod.CardShare.own_card(share, True)
    assert "PHOTO" not in names(qr)
    assert len(qr.encode()) <= mod.MAX_QR_BYTES
    # The long note goes first; the rest still fits
    assert names(qr) == ["FN", "N", "TEL", "EMAIL", "ORG", "TITLE", "ADR", "URL", "BDAY"]


@pytest.mark.parametrize("limit, kept", [
    (160, ["FN", "N", "TEL", "EMAIL", "ORG", "TITLE"]),
    (140, ["FN", "N", "TEL", "EMAIL", "ORG"]),
    (130, ["FN", "N", "TEL", "EMAIL"]),
    # Name, phone and e-mail are never dropped, even when the card stays too large
    (50, ["FN", "N", "TEL", "EMAIL"]),
])
def test_qr_card_drops_fields_in_order(mod, share, monkeypatch, limit, kept):
    share.settings = Settings(fields=list(mod.FIELD_PROPERTIES))
    monkeypatch.setattr(mod, "MAX_QR_BYTES", limit)
    qr = mod.CardShare.own_card(share, True)
    assert names(qr) == kept
    assert len(qr.encode()) <= limit or limit == 50


def test_no_card_chosen(mod, share):
    share.settings = Settings(book="", uid="")
    with pytest.raises(mod.CardError, match="No card chosen"):
        mod.CardShare.own_contact(share)


# ----- the session -----

def test_share_session(mod, share):
    shared = []
    share.is_locked = lambda: False
    share.share_nfc = lambda text: shared.append(text)
    share.inhibit_idle = lambda: share.events.append("inhibit")
    share.uninhibit_idle = lambda: share.events.append("uninhibit")
    share.stop_share_nfc = share.stop_receive_nfc = share.stop_receive_bluetooth = lambda: None
    share.restore_nfc = lambda: share.events.append("restore nfc")
    mod.CardShare.start(share, "sharing")
    assert share.state == "sharing" and share.channels == ["qr", "nfc"] and share.error == ""
    assert share.duration == share.remaining == 30
    assert shared == [mod.CardShare.own_card(share, False)]
    assert share.card_text == mod.CardShare.own_card(share, True)
    assert os.path.dirname(share.workdir) == str(share.tmp / "run")
    open(os.path.join(share.workdir, "card.vcf"), "w").close()
    # One second goes by
    (tick,) = share.glib.of_kind("seconds")
    assert share.glib.fire(tick) is True and share.remaining == 29
    share.remaining = 1
    assert share.glib.fire(tick) is False
    assert share.state == "idle" and share.workdir is None and share.card_text == ""
    assert not os.listdir(share.tmp / "run")
    assert share.events == ["inhibit", "restore nfc", "uninhibit"]


def test_share_without_nfc_still_shows_the_qr_code(mod, share):
    share.is_locked = lambda: False
    share.inhibit_idle = lambda: None

    def no_nfc(text):
        raise mod.CardError("NFC is not available")
    share.share_nfc = no_nfc
    mod.CardShare.start(share, "sharing")
    assert share.channels == ["qr"]
    assert share.error == "NFC is not available, the QR code still works"


def test_receive_session_problems_are_told(mod, share):
    share.is_locked = lambda: False
    share.inhibit_idle = lambda: None
    share.receive_nfc = lambda: None

    def bt():
        raise mod.CardError("Bluetooth is off")
    share.receive_bluetooth = bt
    mod.CardShare.start(share, "receiving")
    assert share.channels == ["nfc"] and share.error == "Bluetooth is off"


def test_nothing_starts_while_locked(mod, share):
    share.is_locked = lambda: True
    with pytest.raises(mod.CardError, match="Unlock"):
        mod.CardShare.start(share, "receiving")
    assert share.state == "idle"


@pytest.mark.parametrize("seconds, duration", [(30, 30), (3, 10), (0, 10), (299, 299),
                                               (100000, 300)])
def test_session_length_is_bounded(mod, share, seconds, duration):
    share.settings = Settings(seconds=seconds)
    share.is_locked = lambda: False
    share.inhibit_idle = lambda: None
    share.receive_nfc = share.receive_bluetooth = lambda: None
    mod.CardShare.start(share, "receiving")
    assert share.duration == share.remaining == duration


def test_extend(mod, share):
    share.state, share.remaining, share.duration = "receiving", 12, 30
    share.channels = ["nfc", "bluetooth"]
    timeouts = []
    share.set_discoverable_timeout = lambda: timeouts.append(share.remaining)
    mod.CardShare.extend(share)
    assert (share.remaining, share.duration, timeouts) == (42, 42, [42])
    share.remaining = 290
    mod.CardShare.extend(share)
    assert share.remaining == 300
    mod.CardShare.extend(share)
    assert share.remaining == 300 and len(timeouts) == 2
    share.state = "idle"
    mod.CardShare.extend(share)
    assert share.remaining == 300


def test_locking_stops_the_session(mod, share):
    stops = []
    share.stop = lambda: stops.append(1)
    mod.CardShare.on_lock_changed(share, None, None, None, None, None, (False,))
    share.state = "sharing"
    mod.CardShare.on_lock_changed(share, None, None, None, None, None, (True,))
    assert stops == [1]


def test_idle_exit_only_without_cards_to_import(mod, share):
    mod.CardShare.schedule_idle_exit(share)
    first = share.idle_id
    assert share.glib.sources[first][1] == mod.IDLE_EXIT_SECONDS
    share.received = [("1", "A", "", card("FN:A"))]
    mod.CardShare.schedule_idle_exit(share)
    assert share.idle_id == 0 and first in share.glib.removed and share.glib.sources == {}


# ----- cards that come in -----

def test_add_received_keeps_a_card_once(mod, share):
    text = card("FN:Bo", "TEL:123")
    mod.CardShare.add_received(share, text, "nfc")
    mod.CardShare.add_received(share, text, "bluetooth")
    mod.CardShare.add_received(share, "hello", "nfc")
    assert share.received == [("1", "Bo", "123", text)]
    assert share.events == [("feedback", "message-new-instant"), ("notify", "1", "Bo", "123")]
    value = mod.CardShare.on_get_property(share, None, None, None, None, "Received")
    assert value.unpack() == [("1", "Bo", "123")]


def test_dismiss_closes_the_notification(mod, share):
    closed = []
    share.close_notification = lambda nid: closed.append(nid)
    share.received = [("1", "A", "", "x"), ("2", "B", "", "y")]
    share.notifications = {41: "1", 42: "2"}
    mod.CardShare.dismiss(share, "1")
    mod.CardShare.dismiss(share, "9")
    assert [r[0] for r in share.received] == ["2"] and closed == [41]
    assert share.signals == [("Received",)]


def test_import_makes_a_new_contact(mod, share):
    added = []
    client = types.SimpleNamespace(add_contact_sync=lambda contact, flags, c: added.append(contact))
    share.registry = types.SimpleNamespace(ref_default_address_book=lambda: types.SimpleNamespace(
        get_uid=lambda: "system-address-book"))
    share.book_client = lambda uid: client if uid == "system-address-book" else None
    share.dismiss = lambda cid: share.events.append(("dismiss", cid))
    text = card("UID:abc", "REV:2026", "FN:Bo", "X-EVOLUTION-FILE-AS:Bo")
    share.received = [("3", "Bo", "", text)]
    assert mod.CardShare.import_card(share, "3") == "Bo"
    (contact,) = added
    assert contact.props["id"] is None
    assert [a.name for a in contact.attributes] == ["FN"]
    assert share.events == [("dismiss", "3")]
    with pytest.raises(mod.CardError):
        mod.CardShare.import_card(share, "4")


class Calls:
    """call() of CardShare answered from a table {(path, method): reply or exception}."""

    def __init__(self, table):
        self.table = table
        self.log = []

    def __call__(self, bus, name, path, iface, method, args=None, reply=None, timeout=5000):
        self.log.append((path, method, args.unpack() if args is not None else None))
        answer = self.table.get((path, method), ())
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_tags_with_a_card(mod, share):
    vcard = card("FN:Tag Card").encode()
    share.call = Calls({
        ("/nfc0/tag0", "GetAll"): (1, True, 1, 8, 0, [], ["/r0", "/r1", "/r2", "/r3"]),
        ("/r0", "GetAll"): (1, 0, 2, [], list(b"Text/VCard"), [], list(vcard)),
        ("/r1", "GetAll"): (1, 0, 1, [], list(b"U"), [], list(b"\x04x")),
        ("/r2", "GetAll"): (1, 0, 2, [], list(b"text/vcard"),
                            [], list(b"BEGIN:VCARD" + bytes(mod.MAX_CARD_BYTES))),
        ("/r3", "GetAll"): fakes.gerror("gone"),
        ("/nfc0/tag1", "GetAll"): fakes.gerror("gone"),
    })
    share.system = None
    added = []
    share.add_received = lambda text, channel: added.append((text, channel))
    mod.CardShare.on_tags(share, ["/nfc0/tag0", "/nfc0/tag1"])
    mod.CardShare.on_tags(share, ["/nfc0/tag0"])  # seen already
    assert added == [(vcard.decode(), "nfc")]
    assert sum(1 for c in share.call.log if c[0] == "/nfc0/tag0") == 1


def test_enable_nfc_waits_for_the_adapter_and_restores(mod, share):
    attempts = []

    def set_prop(bus, name, path, iface, prop, value):
        attempts.append(prop)
        if len(attempts) < 3:
            raise GLib.Error("adapter not there yet")
    share.set_prop = set_prop
    share.call = Calls({("/", "GetEnabled"): (False,), ("/nfc0", "GetMode"): (1,)})
    share.system = None
    mod.CardShare.enable_nfc(share)
    assert share.nfc_was_enabled is False and attempts == ["Powered"] * 3
    assert share.glib.slept == [100000, 100000]
    assert ("/", "SetEnabled", (True,)) in share.call.log
    mod.CardShare.restore_nfc(share)
    assert share.call.log[-1] == ("/", "SetEnabled", (False,))
    assert share.nfc_was_enabled is None


def test_enable_nfc_gives_up(mod, share):
    def set_prop(*a):
        raise GLib.Error("no adapter")
    share.set_prop = set_prop
    share.call = Calls({("/", "GetEnabled"): (True,)})
    share.system = None
    with pytest.raises(mod.CardError):
        mod.CardShare.enable_nfc(share)
    assert len(share.glib.slept) == 20
    # NFC was on: it stays on
    mod.CardShare.restore_nfc(share)
    assert not any(c[1] == "SetEnabled" for c in share.call.log)


# ----- Bluetooth pushes -----

def push(mod, share, transfer, props):
    share.call = Calls({(transfer, "GetAll"): (props,)})
    share.session = types.SimpleNamespace(signal_subscribe=lambda *a: len(share.transfers) + 100,
                                          signal_unsubscribe=lambda sub: None)
    inv = fakes.Invocation()
    mod.CardShare.authorize_push(share, transfer, inv)
    return inv


@pytest.mark.parametrize("state, props, accepted", [
    ("receiving", {"Name": "Anna.VCF", "Type": "", "Size": 300}, True),
    ("receiving", {"Name": "card", "Type": "text/x-vcard", "Size": 300}, True),
    ("receiving", {"Name": "../../evil.sh", "Type": "application/x-sh", "Size": 30}, False),
    ("receiving", {"Name": "big.vcf", "Size": 256 * 1024 + 1}, False),
    ("sharing", {"Name": "a.vcf", "Size": 30}, False),
])
def test_authorize_push(mod, share, state, props, accepted):
    share.state = state
    inv = push(mod, share, "/org/bluez/obex/server/session1/transfer1", props)
    if accepted:
        (target,) = inv.value.unpack()
        assert os.path.dirname(target) == str(share.tmp / "cache" / "obexd" / "plinius-cardshare")
        assert target.endswith(".vcf")
        assert os.stat(os.path.dirname(target)).st_mode & 0o777 == 0o700
    else:
        assert inv.error == ("org.bluez.obex.Error.Rejected", "Only contact cards")
        assert share.transfers == {}


def test_received_transfer_goes_to_the_list_and_its_file_away(mod, share):
    share.state = "receiving"
    inv = push(mod, share, "/t1", {"Name": "a.vcf", "Size": 30})
    (target,) = inv.value.unpack()
    text = card("FN:Bo")
    with open(target, "w") as f:
        f.write(text)
    added = []
    share.add_received = lambda t, channel: added.append((t, channel))
    changed = mod.CardShare.on_transfer_changed
    changed(share, None, None, "/other", None, None, ("x", {"Status": "complete"}, []))
    changed(share, None, None, "/t1", None, None, ("x", {"Status": "active"}, []))
    assert added == [] and os.path.exists(target)
    changed(share, None, None, "/t1", None, None, ("x", {"Status": "complete"}, []))
    assert added == [(text, "bluetooth")] and not os.path.exists(target)
    assert share.transfers == {}


def test_failed_transfer_leaves_nothing(mod, share):
    share.state = "receiving"
    (target,) = push(mod, share, "/t1", {"Name": "a.vcf", "Size": 30}).value.unpack()
    open(target, "w").close()
    share.add_received = lambda *a: pytest.fail("nothing came in")
    mod.CardShare.on_transfer_changed(share, None, None, "/t1", None, None,
                                      ("x", {"Status": "error"}, []))
    assert not os.path.exists(target) and share.transfers == {}


def test_concurrent_pushes_get_their_own_files(mod, share):
    share.state = "receiving"
    first = push(mod, share, "/t1", {"Name": "a.vcf", "Size": 30}).value.unpack()[0]
    second = push(mod, share, "/t2", {"Name": "b.vcf", "Size": 30}).value.unpack()[0]
    open(first, "w").close()
    share.add_received = lambda *a: None
    mod.CardShare.on_transfer_changed(share, None, None, "/t1", None, None,
                                      ("x", {"Status": "complete"}, []))
    third = push(mod, share, "/t3", {"Name": "c.vcf", "Size": 30}).value.unpack()[0]
    assert third != second


def test_bluetooth_visibility_goes_back(mod, share):
    props = []
    share.set_prop = lambda bus, name, path, iface, prop, value: props.append(
        (prop, value.unpack()))
    share.call = Calls({})
    share.session = types.SimpleNamespace(unregister_object=lambda reg: None)
    share.system = None
    share.agent_reg = 5
    share.bt_was_discoverable, share.bt_timeout_before = False, 180
    mod.CardShare.stop_receive_bluetooth(share)
    assert props == [("Discoverable", False), ("DiscoverableTimeout", 180)]
    assert share.call.log == [("/org/bluez/obex", "UnregisterAgent",
                               ("/org/plinius/CardShare/ObexAgent",))]
    # It was visible before: it stays so
    props.clear()
    share.bt_was_discoverable = True
    mod.CardShare.stop_receive_bluetooth(share)
    assert props == []


# ----- D-Bus methods, NFC sharing, Bluetooth receiving -----

def test_method_errors_reach_the_caller(mod, share):
    def refuse(state):
        raise mod.CardError("Unlock the phone first")

    def crash():
        raise KeyError("x")
    share.start = refuse
    share.extend = crash
    share.import_card = lambda cid: f"name of {cid}"
    calls = {}
    for name, params in (("Share", None), ("Extend", None), ("Nope", None),
                         ("Import", GLib.Variant("(s)", ("7",)))):
        inv = fakes.Invocation()
        mod.CardShare.on_method_call(share, None, ":1.5", mod.OBJECT_PATH, mod.BUS_NAME, name,
                                     params, inv)
        calls[name] = inv
    assert calls["Share"].error == ("org.plinius.CardShare.Error", "Unlock the phone first")
    assert calls["Extend"].error[0] == "org.freedesktop.DBus.Error.Failed"
    assert calls["Nope"].error[0] == "org.freedesktop.DBus.Error.UnknownMethod"
    assert calls["Import"].value.unpack() == ("name of 7",)


def test_share_nfc_hands_the_card_to_ndef_share(mod, share, fake_bin, monkeypatch):
    fake_bin.stub("ndef-share")
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    share.enable_nfc = lambda: None
    share.workdir = str(share.tmp / "run")
    text = card("FN:Anna Berg")
    mod.CardShare.share_nfc(share, text)
    path = os.path.join(share.workdir, "card.vcf")
    assert open(path, newline="").read() == text and os.stat(path).st_mode & 0o777 == 0o600
    share.share_proc.wait(timeout=10)
    assert fake_bin.calls("ndef-share") == [["-m", "text/vcard", path]]
    (watch,) = share.glib.of_kind("child")
    assert share.glib.sources[watch][1] == share.share_proc.pid
    mod.CardShare.stop_share_nfc(share)
    assert share.share_proc is None and watch in share.glib.removed


@pytest.mark.parametrize("status, read", [(0, True), (1 << 8, False)])
def test_ndef_share_ending(mod, share, status, read):
    share.state, share.channels = "sharing", ["qr", "nfc"]
    share.share_proc = types.SimpleNamespace(stderr=None)
    emitted = []
    share.session = types.SimpleNamespace(emit_signal=lambda *a: emitted.append(a[3:]))
    stops = []
    share.stop = lambda: stops.append(1)
    mod.CardShare.on_share_exit(share, 1234, status)
    if read:
        assert emitted == [("CardRead", GLib.Variant("(s)", ("nfc",)))] and stops == [1]
        assert ("feedback", "message-sent-instant") in share.events
    else:
        assert emitted == [] and stops == [] and share.channels == ["qr"]
        assert share.error == "NFC is not available, the QR code still works"


def test_receive_bluetooth_makes_the_phone_visible(mod, share):
    props = []
    share.set_prop = lambda bus, name, path, iface, prop, value: props.append(
        (prop, value.unpack()))
    share.get_prop = lambda bus, name, path, iface, prop: {
        "Powered": True, "Alias": "Volla", "Discoverable": False, "DiscoverableTimeout": 180}[prop]
    share.call = Calls({})
    share.session = types.SimpleNamespace(register_object=lambda *a: 9,
                                          unregister_object=lambda reg: None)
    share.agent_node = types.SimpleNamespace(interfaces=[None])
    share.system = None
    share.remaining = 30
    mod.CardShare.receive_bluetooth(share)
    assert share.agent_reg == 9 and share.call.log[0][1] == "RegisterAgent"
    assert props == [("DiscoverableTimeout", 35), ("Discoverable", True)]
    assert (share.bt_was_discoverable, share.bt_timeout_before) == (False, 180)


def test_receive_bluetooth_when_off_or_without_obexd(mod, share):
    share.get_prop = lambda bus, name, path, iface, prop: {"Powered": False}[prop]
    share.system = None
    with pytest.raises(mod.CardError, match="Bluetooth is off"):
        mod.CardShare.receive_bluetooth(share)
    share.get_prop = lambda bus, name, path, iface, prop: {"Powered": True, "Alias": "V"}[prop]
    unregistered = []
    share.session = types.SimpleNamespace(register_object=lambda *a: 4,
                                          unregister_object=unregistered.append)
    share.agent_node = types.SimpleNamespace(interfaces=[None])
    share.call = Calls({("/org/bluez/obex", "RegisterAgent"): GLib.Error("no obexd")})
    with pytest.raises(GLib.Error):
        mod.CardShare.receive_bluetooth(share)
    assert unregistered == [4] and share.agent_reg == 0


# ----- the service on its buses -----

class Bus:
    """A bus connection. Method calls are answered from a table keyed (path, method), for
    Properties.Get/Set (path, "Get"/"Set", property): a reply tuple, a GLib.Error to fail, a
    list of those for one answer per call, or None for a call without a reply. Objects,
    subscriptions and signals are recorded."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.log = []
        self.objects = {}
        self.subscriptions = {}
        self.emitted = []
        self.unregistered = []
        self.unsubscribed = []
        self.next_id = 1

    def _id(self):
        self.next_id += 1
        return self.next_id - 1

    def call_sync(self, name, path, iface, method, args, reply, flags, timeout, cancel):
        params = args.unpack() if args is not None else None
        self.log.append((name, path, method, params))
        key = (path, method, params[1]) if method in ("Get", "Set") else (path, method)
        if key not in self.table:
            raise AssertionError(f"unexpected call {key}")
        answer = self.table[key]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return GLib.Variant(reply.dup_string(), answer) if reply is not None else None

    def calls(self, method):
        return [entry for entry in self.log if entry[2] == method]

    def register_object(self, path, info, method_call, get_property, set_property):
        oid = self._id()
        self.objects[oid] = (path, info.name, method_call, get_property)
        return oid

    def unregister_object(self, oid):
        self.unregistered.append(oid)
        return self.objects.pop(oid, None) is not None

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, handler):
        sid = self._id()
        self.subscriptions[sid] = (sender, iface, member, path, handler)
        return sid

    def signal_unsubscribe(self, sid):
        self.unsubscribed.append(sid)
        self.subscriptions.pop(sid, None)

    def emit_signal(self, dest, path, iface, member, params):
        self.emitted.append((path, iface, member, params.unpack()))

    def handler(self, member, path=None):
        (found,) = [s[4] for s in self.subscriptions.values()
                    if s[2] == member and (path is None or s[3] == path)]
        return found

    def method_call(self, path, method, params=None, sender=":1.40"):
        (found,) = [o for o in self.objects.values() if o[0] == path]
        inv = fakes.Invocation()
        found[2](self, sender, path, found[1], method, params, inv)
        return inv

    def get_property(self, name):
        (found,) = [o for o in self.objects.values() if o[0] == "/org/plinius/CardShare"]
        value = found[3](self, ":1.40", found[0], found[1], name)
        return None if value is None else value.unpack()


class ServiceSettings(Settings):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.handlers = []

    def connect(self, signal, handler):
        self.handlers.append((signal, handler))


class Book:
    def __init__(self, contacts=(), fail=None):
        self.contacts = list(contacts)
        self.fail = fail
        self.added = []

    def get_contacts_sync(self, query, cancellable):
        if self.fail:
            raise self.fail
        return True, list(self.contacts)

    def get_contact_sync(self, uid, cancellable):
        for contact in self.contacts:
            if contact.get_property("id") == uid:
                return True, contact
        raise GLib.Error("Contact not found")

    def add_contact_sync(self, contact, flags, cancellable):
        self.added.append(contact)


class Source:
    def __init__(self, uid):
        self.uid = uid

    def get_uid(self):
        return self.uid


class Registry:
    def __init__(self, books, default="system-address-book"):
        self.books = books
        self.default = default

    def list_enabled(self, extension):
        assert extension == "Address Book"
        return [Source(uid) for uid in self.books]

    def ref_source(self, uid):
        return Source(uid) if uid in self.books else None

    def ref_default_address_book(self):
        return Source(self.default)


def own_contact(**props):
    return Contact([a.copy() for a in OWN], dict({"full-name": "Anna Berg", "id": "me"}, **props))


def evolution(books, registries):
    """EDataServer and EBook over the given address books {uid: Book}."""
    connects = []

    def new_sync(cancellable):
        registries.append(Registry(books))
        if isinstance(books, Exception):
            raise books
        return registries[-1]

    def connect_sync(source, timeout, cancellable):
        connects.append((source.get_uid(), timeout))
        return books[source.get_uid()]

    eds = types.SimpleNamespace(SourceRegistry=types.SimpleNamespace(new_sync=new_sync),
                                SOURCE_EXTENSION_ADDRESS_BOOK="Address Book")
    ebook = types.SimpleNamespace(BookClient=types.SimpleNamespace(connect_sync=connect_sync),
                                  BookOperationFlags=types.SimpleNamespace(NONE=0))
    return eds, ebook, connects


DESKTOP_CALLS = {
    ("/org/gnome/ScreenSaver", "GetActive"): (False,),
    ("/org/gnome/SessionManager", "Inhibit"): (77,),
    ("/org/gnome/SessionManager", "Uninhibit"): None,
    ("/org/sigxcpu/Feedback", "TriggerFeedback"): (1,),
    ("/org/freedesktop/Notifications", "Notify"): (5,),
    ("/org/freedesktop/Notifications", "CloseNotification"): None,
    ("/org/bluez/obex", "RegisterAgent"): None,
    ("/org/bluez/obex", "UnregisterAgent"): None,
}

RADIOS = {
    ("/", "GetEnabled"): (False,),
    ("/", "SetEnabled"): None,
    ("/nfc0", "Set", "Powered"): None,
    ("/nfc0", "GetMode"): (1,),
    ("/nfc0", "GetTags"): ([],),
    ("/org/bluez/hci0", "Get", "Powered"): (GLib.Variant("b", True),),
    ("/org/bluez/hci0", "Get", "Alias"): (GLib.Variant("s", "Volla"),),
    ("/org/bluez/hci0", "Get", "Discoverable"): (GLib.Variant("b", False),),
    ("/org/bluez/hci0", "Get", "DiscoverableTimeout"): (GLib.Variant("u", 180),),
    ("/org/bluez/hci0", "Set", "Discoverable"): None,
    ("/org/bluez/hci0", "Set", "DiscoverableTimeout"): None,
}


@pytest.fixture
def service(mod, tmp_path, monkeypatch):
    """A CardShare made by its constructor on fake buses, with an address book holding the
    own card; returns it with .session, .system, .glib and .tmp."""
    glib = fakes.FakeGLib(get_user_runtime_dir=lambda: str(tmp_path / "run"),
                          get_user_cache_dir=lambda: str(tmp_path / "cache"))
    (tmp_path / "run").mkdir()
    monkeypatch.setattr(mod, "GLib", glib)
    session, system = Bus(DESKTOP_CALLS), Bus(RADIOS)
    settings = ServiceSettings()
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(
        Gio, bus_get_sync=lambda kind: system if kind == Gio.BusType.SYSTEM else session,
        Settings=types.SimpleNamespace(new=lambda schema: settings)))
    books = {"book": Book([own_contact()]), "system-address-book": Book()}
    registries = []
    eds, ebook, connects = evolution(books, registries)
    monkeypatch.setattr(mod, "EDataServer", eds)
    monkeypatch.setattr(mod, "EBook", ebook)
    loop = types.SimpleNamespace(quits=0)
    loop.quit = lambda: setattr(loop, "quits", loop.quits + 1)
    s = mod.CardShare(loop)
    s.session, s.system, s.glib, s.tmp = session, system, glib, tmp_path
    s.books_, s.registries, s.connects = books, registries, connects
    return s


def test_service_takes_its_place_on_the_session_bus(mod, service):
    ((path, iface, _m, _g),) = service.session.objects.values()
    assert (path, iface) == ("/org/plinius/CardShare", "org.plinius.CardShare")
    assert sorted(s[2] for s in service.session.subscriptions.values()) == [
        "ActionInvoked", "ActiveChanged", "NotificationClosed"]
    assert service.settings.handlers == [("changed", service.on_settings_changed)]
    # Nothing to do: it exits after a while, and D-Bus starts it again
    (sid,) = service.glib.of_kind("seconds")
    assert service.glib.sources[sid][1] == mod.IDLE_EXIT_SECONDS
    service.glib.fire(sid)
    assert service.loop.quits == 1


def test_properties_over_dbus(mod, service):
    get = service.session.get_property
    assert get("State") == "idle" and get("Remaining") == get("Duration") == 0
    assert get("CardText") == "" and get("Error") == ""
    assert get("Channels") == [] and get("Received") == []
    assert get("CardName") == "Anna Berg" and get("BluetoothName") == "Volla"
    assert get("Nope") is None
    # The address book is asked once and kept, without waiting for a remote book
    service.session.get_property("CardName")
    assert service.connects == [("book", 0xFFFFFFFF)] and len(service.registries) == 1


def test_card_name_empty_without_a_card(mod, service):
    service.settings.values["contact-uid"] = "someone-deleted"
    assert service.session.get_property("CardName") == ""
    service.settings.values["contact-book"] = "removed-book"
    service.books.clear()
    assert service.session.get_property("CardName") == ""
    with pytest.raises(mod.CardError, match="address book of the card is gone"):
        service.own_contact()
    service.settings.values["contact-book"] = "book"
    with pytest.raises(mod.CardError, match="contact of the card is gone"):
        service.own_contact()


@pytest.mark.parametrize("answers, name", [
    ({("/org/bluez/hci0", "Get", "Powered"): (GLib.Variant("b", False),)}, ""),
    ({("/org/bluez/hci0", "Get", "Powered"): GLib.Error("no bluez")}, ""),
])
def test_bluetooth_name_when_off_or_missing(mod, service, answers, name):
    service.system.table.update(answers)
    assert service.session.get_property("BluetoothName") == name


def test_changed_sends_the_values(mod, service):
    service.state, service.remaining = "receiving", 12
    service.changed("State", "Remaining")
    assert service.session.emitted == [(
        "/org/plinius/CardShare", "org.freedesktop.DBus.Properties", "PropertiesChanged",
        ("org.plinius.CardShare", {"State": "receiving", "Remaining": 12}, []))]


def test_settings_change_of_the_card_is_told(mod, service):
    service.on_settings_changed(service.settings, "fields")
    assert service.session.emitted == []
    service.on_settings_changed(service.settings, "contact-uid")
    assert [e[3][1] for e in service.session.emitted] == [{"CardName": "Anna Berg"}]


def test_list_contacts_of_all_readable_books(mod, service, capsys):
    service.books_.clear()
    service.books_.update({
        "work": Book([Contact([], {"full-name": "zoe Zander", "id": "z"}),
                      Contact([], {"full-name": "", "id": "nameless"})]),
        "broken": Book(fail=GLib.Error("offline")),
        "home": Book([Contact([], {"full-name": "Anna Berg", "id": "a"})]),
    })
    inv = service.session.method_call("/org/plinius/CardShare", "ListContacts")
    assert inv.value.unpack() == ([("home", "a", "Anna Berg"), ("work", "z", "zoe Zander")],)
    assert "Address book broken not readable: offline" in capsys.readouterr().err
    # Asked again: the same registry and clients
    service.session.method_call("/org/plinius/CardShare", "ListContacts")
    assert len(service.registries) == 1 and [c[0] for c in service.connects] == [
        "work", "broken", "home"]


def test_methods_over_dbus(mod, service):
    call = service.session.method_call
    preview = call("/org/plinius/CardShare", "PreviewCard", GLib.Variant("(b)", (True,)))
    assert preview.value.unpack() == (service.own_card(True),)
    service.received = [("1", "Bo", "", card("FN:Bo"))]
    assert call("/org/plinius/CardShare", "Dismiss", GLib.Variant("(s)", ("1",))).answered == 1
    assert service.received == []
    imported = call("/org/plinius/CardShare", "Import", GLib.Variant("(s)", ("1",)))
    assert imported.error == ("org.plinius.CardShare.Error", "This card is no longer there")
    for method in ("Extend", "Stop"):  # nothing runs: nothing happens
        assert call("/org/plinius/CardShare", method).value is None
    assert service.state == "idle"


def test_method_failing_in_the_libraries_is_returned(mod, service, monkeypatch, capsys):
    eds, ebook, _ = evolution(GLib.Error("no evolution-data-server"), [])
    monkeypatch.setattr(mod, "EDataServer", eds)
    inv = service.session.method_call("/org/plinius/CardShare", "ListContacts")
    assert inv.error == ("org.plinius.CardShare.Error", "no evolution-data-server")
    assert "ListContacts failed: no evolution-data-server" in capsys.readouterr().err


def test_import_into_the_default_address_book(mod, service):
    text = card("UID:abc", "FN:Bo", "TEL:123")
    service.received = [("4", "Bo", "123", text)]
    service.notifications = {9: "4"}
    inv = service.session.method_call("/org/plinius/CardShare", "Import",
                                      GLib.Variant("(s)", ("4",)))
    assert inv.value.unpack() == ("Bo",)
    (contact,) = service.books_["system-address-book"].added
    assert contact.props["id"] is None and [a.name for a in contact.attributes] == ["FN", "TEL"]
    assert service.received == [] and service.notifications == {}
    assert service.session.calls("CloseNotification")[-1][3] == (9,)


# ----- a whole receiving session over the real call paths -----

def test_receive_session_on_the_buses(mod, service):
    system, session = service.system, service.session
    session.method_call("/org/plinius/CardShare", "Receive")
    assert service.state == "receiving" and service.channels == ["nfc", "bluetooth"]
    assert service.nfc_was_enabled is False and service.inhibit_cookie == 77
    assert system.log[:3] == [
        ("org.sailfishos.nfc.settings", "/", "GetEnabled", None),
        ("org.sailfishos.nfc.settings", "/", "SetEnabled", (True,)),
        ("org.neard", "/nfc0", "Set", ("org.neard.Adapter", "Powered", True))]
    assert [c[3] for c in system.calls("Set")[1:]] == [
        ("org.bluez.Adapter1", "DiscoverableTimeout", 35),
        ("org.bluez.Adapter1", "Discoverable", True)]
    assert session.calls("RegisterAgent")[0][3] == ("/org/plinius/CardShare/ObexAgent",)
    # A tag with a card is held to the phone
    vcard = card("FN:Bo", "TEL:123")
    system.table.update({
        ("/nfc0/tag0", "GetAll"): (1, True, 1, 8, 0, [], ["/nfc0/tag0/ndef0"]),
        ("/nfc0/tag0/ndef0", "GetAll"): (1, 0, 2, [], b"text/vcard", b"", vcard.encode()),
    })
    system.handler("TagsChanged")(system, ":1.2", "/nfc0", "org.sailfishos.nfc.Adapter",
                                  "TagsChanged", GLib.Variant("(ao)", (["/nfc0/tag0"],)))
    assert service.session.get_property("Received") == [("1", "Bo", "123")]
    assert session.calls("TriggerFeedback")[0][3][1] == "message-new-instant"
    (notify,) = session.calls("Notify")
    assert notify[3][3:6] == ("Bo", "123", ["import", "Add to Contacts", "dismiss", "Dismiss"])
    assert service.notifications == {5: "1"}
    # The OBEX agent: only pushes are looked at
    agent = session.method_call("/org/plinius/CardShare/ObexAgent", "Release")
    assert agent.answered == 1 and agent.value is None
    session.table[("/t1", "GetAll")] = ({"Name": GLib.Variant("s", "x.vcf"),
                                         "Size": GLib.Variant("t", 20)},)
    push = session.method_call("/org/plinius/CardShare/ObexAgent", "AuthorizePush",
                               GLib.Variant("(o)", ("/t1",)))
    (target,) = push.value.unpack()
    assert os.path.dirname(target).endswith("obexd/plinius-cardshare")
    # +30 s also keeps the phone visible longer
    service.remaining = 20
    session.method_call("/org/plinius/CardShare", "Extend")
    assert system.calls("Set")[-1][3] == ("org.bluez.Adapter1", "DiscoverableTimeout", 55)
    # Receive again while receiving: one fresh session
    session.method_call("/org/plinius/CardShare", "Receive")
    assert len(session.calls("RegisterAgent")) == 2 and len(session.calls("UnregisterAgent")) == 1
    # Locking the phone ends it, and everything goes back
    tick = service.tick_id
    workdir = service.workdir
    session.handler("ActiveChanged")(session, ":1.3", "/org/gnome/ScreenSaver",
                                     "org.gnome.ScreenSaver", "ActiveChanged", (True,))
    assert service.state == "idle" and tick in service.glib.removed
    assert not os.path.exists(workdir) and service.tags_sub == 0
    assert system.log[-3:] == [
        ("org.bluez", "/org/bluez/hci0", "Set", ("org.bluez.Adapter1", "Discoverable", False)),
        ("org.bluez", "/org/bluez/hci0", "Set",
         ("org.bluez.Adapter1", "DiscoverableTimeout", 180)),
        ("org.sailfishos.nfc.settings", "/", "SetEnabled", (False,))]
    assert session.calls("Uninhibit")[-1][3] == (77,)
    # A card waits to be imported: no idle exit
    assert service.idle_id == 0


def test_receive_session_when_nfc_and_the_lock_query_fail(mod, service, capsys):
    service.system.table[("/", "GetEnabled")] = GLib.Error("nfcd not running")
    service.session.table[("/org/gnome/ScreenSaver", "GetActive")] = GLib.Error("no phosh")
    service.system.table[("/org/bluez/hci0", "Get", "Discoverable")] = (GLib.Variant("b", True),)
    service.start("receiving")
    assert service.channels == ["bluetooth"] and service.error == "NFC is not available"
    assert "NFC reading failed: nfcd not running" in capsys.readouterr().err
    # Visible already: left as it is, now and afterwards
    assert service.system.calls("Set") == []
    service.stop()
    assert service.system.calls("Set") == []


def test_enable_nfc_when_on_and_not_polling(mod, service, capsys):
    service.system.table[("/", "GetEnabled")] = (True,)
    service.system.table[("/nfc0", "GetMode")] = [GLib.Error("starting")] + [(0,)] * 2
    service.enable_nfc()
    service.enable_nfc()  # a second time keeps what it was at first
    assert service.nfc_was_enabled is True and service.system.calls("SetEnabled") == []
    assert len(service.glib.slept) == 60
    assert capsys.readouterr().err.count("NFC does not poll yet, going on anyway") == 2


def test_stop_with_everything_failing_still_ends(mod, service, capsys):
    service.start("receiving")
    for key in (("/org/bluez/obex", "UnregisterAgent"), ("/org/gnome/SessionManager", "Uninhibit")):
        service.session.table[key] = GLib.Error("gone")
    for key in (("/org/bluez/hci0", "Set", "Discoverable"), ("/", "SetEnabled")):
        service.system.table[key] = GLib.Error("gone")
    service.bt_timeout_before = None
    service.stop()
    err = capsys.readouterr().err
    for message in ("Unregistering the OBEX agent failed: gone",
                    "Hiding Bluetooth again failed: gone",
                    "Switching NFC off again failed: gone", "Releasing the screen failed: gone"):
        assert message in err
    assert service.state == "idle" and service.agent_reg == 0 and service.inhibit_cookie == 0
    assert service.nfc_was_enabled is None and service.bt_was_discoverable is None


def test_screen_feedback_and_notifications_failing_are_logged(mod, service, capsys):
    for key in (("/org/gnome/SessionManager", "Inhibit"),
                ("/org/sigxcpu/Feedback", "TriggerFeedback"),
                ("/org/freedesktop/Notifications", "Notify"),
                ("/org/freedesktop/Notifications", "CloseNotification")):
        service.session.table[key] = GLib.Error("not there")
    service.inhibit_idle()
    service.feedback("message-new-instant")
    service.notify_received("1", "Bo", "")
    service.notifications = {3: "1"}
    service.close_notification(3)
    err = capsys.readouterr().err
    assert "Keeping the screen on failed: not there" in err
    assert "Feedback failed: not there" in err and "Notification failed: not there" in err
    assert service.inhibit_cookie == 0 and service.notifications == {}
    # Without a number or e-mail the notification says what came in
    service.session.table[("/org/freedesktop/Notifications", "Notify")] = (6,)
    service.notify_received("2", "Bo", "")
    assert service.session.calls("Notify")[-1][3][4] == "A contact card came in"
    assert service.notifications == {6: "2"}


def test_notification_actions(mod, service, capsys):
    session = service.session
    action, closed = session.handler("ActionInvoked"), session.handler("NotificationClosed")
    service.received = [("1", "Bo", "", card("FN:Bo")), ("2", "Al", "", card("FN:Al"))]
    service.notifications = {11: "1", 12: "2", 13: "3"}
    action(session, ":1.5", None, None, "ActionInvoked", (99, "import"))  # not ours
    action(session, ":1.5", None, None, "ActionInvoked", (12, "dismiss"))
    assert [r[0] for r in service.received] == ["1"]
    action(session, ":1.5", None, None, "ActionInvoked", (11, "import"))
    assert service.received == [] and len(service.books_["system-address-book"].added) == 1
    action(session, ":1.5", None, None, "ActionInvoked", (13, "import"))  # gone meanwhile
    assert "Notification action failed: This card is no longer there" in capsys.readouterr().err
    service.notifications = {14: "4"}
    action(session, ":1.5", None, None, "ActionInvoked", (14, "default"))
    closed(session, ":1.5", None, None, "NotificationClosed", (15, 2))
    assert service.notifications == {}
    service.notifications = {16: "5"}
    closed(session, ":1.5", None, None, "NotificationClosed", (16, 2))
    assert service.notifications == {}
    # Nothing waits any more: the idle exit is scheduled again
    assert service.idle_id in service.glib.sources


def test_shutdown_stops_and_closes_the_notifications(mod, service):
    service.start("receiving")
    service.notifications = {21: "1", 22: "2"}
    service.shutdown()
    assert service.state == "idle"
    assert [c[3] for c in service.session.calls("CloseNotification")] == [(21,), (22,)]


def test_extend_without_bluetooth_leaves_it_alone(mod, service):
    service.system.table[("/org/bluez/hci0", "Get", "Powered")] = (GLib.Variant("b", False),)
    service.start("receiving")
    assert service.channels == ["nfc"] and service.error == "Bluetooth is off"
    sets = len(service.system.calls("Set"))
    service.extend()
    assert service.remaining == 60 and len(service.system.calls("Set")) == sets


def test_share_session_ends_when_the_card_was_read(mod, service, fake_bin, monkeypatch):
    fake_bin.stub("ndef-share", 'echo "reader went away" >&2; exit 0')
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    service.session.method_call("/org/plinius/CardShare", "Share")
    assert service.state == "sharing" and service.channels == ["qr", "nfc"]
    proc = service.share_proc
    (watch,) = service.glib.of_kind("child")
    proc.wait(timeout=10)
    service.glib.fire(watch, proc.pid, 0)
    assert service.state == "idle" and service.share_proc is None
    assert ("/org/plinius/CardShare", "org.plinius.CardShare", "CardRead", ("nfc",)) in \
        service.session.emitted
    assert service.session.calls("TriggerFeedback")[0][3][1] == "message-sent-instant"
    assert proc.stderr.closed


def test_share_exit_after_the_session(mod, service):
    service.on_share_exit(1, 0)  # nothing ran
    proc = types.SimpleNamespace(stderr=None)
    service.share_proc = proc
    service.on_share_exit(1, 0)  # ended after a stop: nothing to tell
    assert service.session.emitted == [] and service.state == "idle"


class StubbornProc:
    """An ndef-share that does not end on SIGTERM."""

    def __init__(self, stderr=None):
        self.signals = []
        self.waits = []
        self.killed = False
        self.stderr = stderr

    def poll(self):
        return None

    def send_signal(self, signum):
        self.signals.append(signum)

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if timeout is not None:
            raise mod_subprocess.TimeoutExpired("ndef-share", timeout)
        return -9

    def kill(self):
        self.killed = True


def test_stop_share_kills_what_does_not_end(mod, service):
    proc = service.share_proc = StubbornProc()
    service.share_watch = 0
    service.stop_share_nfc()
    assert proc.signals == [mod_signal.SIGTERM] and proc.waits == [3, None] and proc.killed
    service.stop_share_nfc()  # nothing left
    assert service.share_proc is None


def test_transfer_unreadable_or_its_file_gone(mod, service, capsys):
    service.state = "receiving"
    service.session.table[("/t9", "GetAll")] = GLib.Error("transfer gone")
    inv = fakes.Invocation()
    service.authorize_push("/t9", inv)
    assert inv.error == ("org.bluez.obex.Error.Rejected", "unreadable")
    assert "Transfer unreadable: transfer gone" in capsys.readouterr().err
    # obexd says complete, but the file is not there
    sub = service.session.signal_subscribe(None, None, "PropertiesChanged", "/t8", None, 0, None)
    service.transfers["/t8"] = (str(service.tmp / "missing.vcf"), sub)
    service.on_transfer_changed(None, None, "/t8", None, None, ("x", {"Status": "complete"}, []))
    assert "Received card unreadable:" in capsys.readouterr().err
    assert service.transfers == {} and sub in service.session.unsubscribed


def test_add_received_skips_an_unreadable_card(mod, service, capsys):
    service.add_received("BEGIN:VCARD but not one", "nfc")
    assert service.received == []
    assert "Unreadable card over nfc, ignored" in capsys.readouterr().err
    service.add_received(card("FN:Al"), "nfc")
    service.add_received(card("FN:Bo"), "bluetooth")
    assert [r[1] for r in service.received] == ["Al", "Bo"]


def test_qr_card_with_fields_that_cannot_be_dropped(mod, service, monkeypatch):
    service.settings.values["fields"] = ["name", "phone"]
    monkeypatch.setattr(mod, "MAX_QR_BYTES", 10)
    assert names(service.own_card(True)) == ["FN", "N", "TEL"]


def test_stop_without_a_workdir(mod, service):
    service.state = "receiving"
    # Hidden before, with no timeout known: only the visibility goes back
    service.bt_was_discoverable, service.bt_timeout_before = False, None
    service.stop("NFC went away")
    assert service.state == "idle" and service.error == "NFC went away"
    assert [c[3] for c in service.system.calls("Set")] == [
        ("org.bluez.Adapter1", "Discoverable", False)]


# ----- the program as D-Bus starts it -----

@pytest.mark.parametrize("acquired", [True, False])
def test_program_owns_its_name_and_shuts_down(mod, acquired):
    session, system = Bus(DESKTOP_CALLS), Bus(RADIOS)
    settings = ServiceSettings()
    owned, signals, quits = [], {}, []

    def bus_own_name(kind, name, flags, acquired_cb, name_acquired_cb, lost_cb):
        owned.append((kind, name, acquired_cb, lost_cb))

    class Loop:
        def run(self):
            (_kind, name, acquired_cb, lost_cb), = owned
            assert name == "org.plinius.CardShare"
            if acquired:
                acquired_cb(session, name)
                # The service is on the bus; a card's notification is still open
                (svc,) = [o[2].__self__ for o in session.objects.values()]
                svc.notifications = {31: "1"}
            lost_cb(session, name)
            assert signals[mod_signal.SIGTERM]() is False

        def quit(self):
            quits.append(1)

    gio = fakes.Proxy(Gio, bus_own_name=bus_own_name,
                      bus_get_sync=lambda kind: system if kind == Gio.BusType.SYSTEM else session,
                      Settings=types.SimpleNamespace(new=lambda schema: settings))
    glib = fakes.FakeGLib(MainLoop=Loop)
    unix = types.SimpleNamespace(signal_add=lambda prio, signum, handler: signals.setdefault(
        signum, handler))
    eds, ebook, _ = evolution({}, [])
    code = run_as_main(CARDSHARE, Gio=gio, GLib=glib, GLibUnix=unix, EDataServer=eds,
                       EBook=ebook, EBookContacts=EBOOK_CONTACTS)
    assert code is None and len(quits) == 2 and set(signals) == {mod_signal.SIGTERM,
                                                                 mod_signal.SIGINT}
    closed = [c[3] for c in session.calls("CloseNotification")]
    assert closed == ([(31,)] if acquired else [])
