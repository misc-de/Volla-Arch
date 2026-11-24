# NFC Lab (plinius-nfc-lab): hex parsing, NDEF records read and written, the Type 2 TLV, APDU
# cases and what goes to nfcd. GTK/Adw are faked; nfcd is a fake bus.

import types

import pytest
from gi.repository import GLib

import desktop_fakes as fakes
from conftest import PKG
from desktop_mainrun import run_as_main

NFCLAB = PKG / "plinius-nfc-lab" / "nfc-lab.py"


@pytest.fixture(scope="module")
def mod():
    return fakes.load_gui(NFCLAB, "nfc_lab_under_test")


def test_hexs(mod):
    assert mod.hexs(b"\x00\x0a\xff") == "00 0A FF"
    assert mod.hexs(b"") == ""


@pytest.mark.parametrize("text, data", [
    ("00 A4 04 00", b"\x00\xa4\x04\x00"), ("00a40400", b"\x00\xa4\x04\x00"),
    ("de:ad:BE:ef", b"\xde\xad\xbe\xef"), ("", b""),
])
def test_parse_hex(mod, text, data):
    assert mod.parse_hex(text) == data


def test_parse_hex_odd(mod):
    with pytest.raises(ValueError, match="odd"):
        mod.parse_hex("A40")


# ----- reading NDEF records -----

@pytest.mark.parametrize("tnf, rtype, payload, line", [
    (1, b"U", b"\x04plinius.cais.de", ("Link", "https://plinius.cais.de")),
    (1, b"U", b"\x02example.org/a", ("Link", "https://www.example.org/a")),
    (1, b"U", b"\x05+491701234567", ("Link", "tel:+491701234567")),
    (1, b"U", b"\x00urn:x", ("Link", "urn:x")),
    (1, b"U", b"\x23nfc:x", ("Link", "urn:nfc:nfc:x")),
    (1, b"U", b"\x7fodd", ("Link", "odd")),  # an unknown prefix code adds nothing
    (1, b"T", b"\x02enHello", ("Text (en)", "Hello")),
    (1, b"T", b"\x05de-DE" + "Grüß".encode(), ("Text (de-DE)", "Grüß")),
    (2, b"text/vcard", b"BEGIN:VCARD", ("MIME text/vcard", "BEGIN:VCARD")),
    (4, b"android.com:pkg", b"\x01\x02", ("External android.com:pkg", "01 02")),
    (1, b"Sp", b"\xd1", ("Record (TNF 1, type Sp)", "D1")),
    (0, b"", b"", ("Record (TNF 0, type )", "")),
])
def test_decode_ndef(mod, tnf, rtype, payload, line):
    assert mod.decode_ndef(tnf, rtype, payload) == line


def test_decode_ndef_utf16_text_with_bom(mod):
    payload = b"\x82en" + "Hi".encode("utf-16")  # with the BOM Python writes
    assert mod.decode_ndef(1, b"T", payload) == ("Text (en)", "Hi")


def test_decode_ndef_utf16_text_without_bom_is_big_endian(mod):
    payload = b"\x82en" + "Hi".encode("utf-16-be")
    assert mod.decode_ndef(1, b"T", payload) == ("Text (en)", "Hi")


# ----- writing -----

@pytest.mark.parametrize("value, code, rest", [
    ("https://www.example.org", 0x02, b"example.org"),   # the longest prefix wins
    ("https://example.org", 0x04, b"example.org"),
    ("http://www.a.b", 0x01, b"a.b"),
    ("tel:+4912345", 0x05, b"+4912345"),
    ("urn:epc:id:x", 0x1E, b"x"),
    ("urn:nfc:wkt", 0x23, b"wkt"),
    ("geo:1,2", 0x00, b"geo:1,2"),
])
def test_ndef_link(mod, value, code, rest):
    msg = mod.ndef_message("link", value)
    payload = bytes([code]) + rest
    assert msg == bytes([0xD1, 1, len(payload)]) + b"U" + payload
    # What is written reads back the same
    assert mod.decode_ndef(1, msg[3:4], msg[4:]) == ("Link", value)


def test_ndef_text(mod):
    msg = mod.ndef_message("text", "Grüße")
    payload = b"\x02en" + "Grüße".encode()
    assert msg == bytes([0xD1, 1, len(payload)]) + b"T" + payload
    assert mod.decode_ndef(1, b"T", msg[4:]) == ("Text (en)", "Grüße")


def test_ndef_too_long(mod):
    mod.ndef_message("text", "x" * 252)  # 3 + 252 = 255 fits a short record
    with pytest.raises(ValueError, match="too long"):
        mod.ndef_message("text", "x" * 253)
    with pytest.raises(ValueError):
        mod.ndef_message("text", "ü" * 127)  # 254 bytes of UTF-8 + 3


def test_type2_tlv(mod):
    msg = b"\xd1\x01\x02T\x02e"
    assert mod.type2_tlv(msg) == b"\x03\x06" + msg + b"\xfe"
    long_msg = bytes(300)
    assert mod.type2_tlv(long_msg) == b"\x03\xff\x01\x2c" + long_msg + b"\xfe"
    edge = bytes(0xFF)
    assert mod.type2_tlv(edge)[:4] == b"\x03\xff\x00\xff"
    assert mod.type2_tlv(bytes(0xFE))[:2] == b"\x03\xfe"


def test_apdu_presets_are_well_formed(mod):
    for _label, apdu in mod.APDU_PRESETS:
        data = mod.parse_hex(apdu)
        assert len(data) >= 4
        if len(data) > 5:
            assert len(data) in (5 + data[4], 6 + data[4])


# ----- the app against nfcd -----

class Lab:
    """The NfcLab methods on a plain object with a recording call()."""

    def __init__(self, mod, sync=None):
        self.mod = mod
        self.calls = []
        self.logs = []
        self.toasts = []
        self.sync = sync or {}
        self.tag_path = None
        self.cleared = 0
        self.shown = []

    def call(self, path, iface, method, params=None, reply="()", name=None, done=None):
        # "ay" unpacks as a list of ints; bytes compare more readably
        args = tuple(bytes(v) if isinstance(v, list) else v
                     for v in params.unpack()) if params else None
        self.calls.append((path, iface, method, args, reply))
        self.done = done

    def call_sync(self, path, iface, method, params=None, reply="()", name=None):
        answer = self.sync[(path, method)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def log(self, text, direction=""):
        self.logs.append((direction, text))

    def toast(self, text):
        self.toasts.append(text)

    def __getattr__(self, name):
        return getattr(self.mod.NfcLab, name).__get__(self)


@pytest.mark.parametrize("apdu, sent", [
    ("00 84 00 00 08", (0x00, 0x84, 0, 0, b"", 8)),                       # case 2
    ("00 B0 00 00 00", (0x00, 0xB0, 0, 0, b"", 256)),                     # Le 0 = 256
    ("00 A4 00 0C 02 3F 00", (0x00, 0xA4, 0, 0x0C, b"\x3f\x00", 0)),      # case 3
    ("00A4040007D276000085010100", (0x00, 0xA4, 4, 0, bytes.fromhex("D2760000850101"), 256)),
    ("80 CA 9F 7F", (0x80, 0xCA, 0x9F, 0x7F, b"", 0)),                    # case 1
])
def test_send_apdu_cases(mod, apdu, sent):
    lab = Lab(mod)
    lab.send_apdu("/nfc0/tag0", mod.parse_hex(apdu))
    ((path, iface, method, params, reply),) = lab.calls
    assert (iface, method, reply) == ("org.sailfishos.nfc.IsoDep", "Transmit", "(ayyy)")
    assert params == sent
    assert lab.logs == [("→", mod.hexs(mod.parse_hex(apdu)))]


def test_send_apdu_logs_the_answer(mod):
    lab = Lab(mod)
    lab.send_apdu("/t", mod.parse_hex("00 84 00 00 04"))
    lab.done((b"AB\x01\xff", 0x90, 0x00), None)
    assert lab.logs[1:] == [("←", "41 42 01 FF  SW 9000"), ("", "  AB..")]
    lab.done(None, fakes.gerror("tag gone"))
    assert lab.logs[-1] == ("←", "failed: tag gone")


def test_send_apdu_too_short_and_not_hex(mod):
    lab = Lab(mod)
    lab.send_apdu("/t", b"\x00\xa4\x04")
    lab.send_apdu_text("/t", "00 A")
    assert lab.calls == [] and lab.toasts == ["An APDU has at least 4 bytes",
                                              "Not an APDU: odd number of hex digits"]


def test_send_raw(mod):
    lab = Lab(mod)
    lab.send_raw("/t", "30 00")
    assert lab.calls == [("/t", "org.sailfishos.nfc.Tag", "Transceive", (b"\x30\x00",), "(ay)")]
    lab.done((b"\x04\x11",), None)
    assert lab.logs[-1] == ("←", "04 11")
    lab.send_raw("/t", "zz 3")
    assert lab.toasts == ["Not hex: odd number of hex digits"]


def test_read_type2_logs_16_bytes_a_line(mod):
    lab = Lab(mod)
    lab.read_type2("/t")
    lab.done((bytes(range(20)),), None)
    assert lab.logs[1:] == [("←", "0000  " + mod.hexs(bytes(range(16)))),
                            ("←", "0010  10 11 12 13")]


def test_show_tags_follows_the_first_tag(mod):
    lab = Lab(mod)
    lab.clear_tag_groups = lambda: setattr(lab, "cleared", lab.cleared + 1)
    lab.show_tag = lambda path: lab.shown.append(path)
    lab.show_tags(["/nfc0/tag1", "/nfc0/tag2"])
    lab.show_tags(["/nfc0/tag2", "/nfc0/tag1"])  # still there: nothing changes
    lab.show_tags([])
    lab.show_tags(["/nfc0/tag3"])
    assert lab.shown == ["/nfc0/tag1", "/nfc0/tag3"] and lab.cleared == 3
    assert lab.logs == [("", "Tag gone")]


def test_show_ndef_reads_each_record(mod):
    lab = Lab(mod, sync={
        ("/r0", "GetAll"): (1, 0, 1, [], b"U", b"", b"\x04plinius.cais.de"),
        ("/r1", "GetAll"): fakes.gerror("gone"),
    })
    rows = []
    lab.add_group = lambda title, description=None: rows.append((title, description)) or title
    lab.info_row = lambda group, title, value: rows.append((group, title, value))
    lab.show_ndef(["/r0", "/r1"])
    assert rows == [("NDEF", None), ("NDEF", "Link", "https://plinius.cais.de")]
    assert lab.logs == [("", "NDEF Link: https://plinius.cais.de"), ("", "Reading /r1 failed: gone")]


def test_refresh_adapter_states(mod):
    statuses = []
    for enabled, powered, expected in ((False, False, ("NFC is off", "", True)),
                                       (True, False, ("NFC is on",)),
                                       (True, True, ("Hold a tag or card to the back of the phone",))):
        lab = Lab(mod, sync={("/nfc0", "GetAll"): (1, enabled, powered, 0, 0, True, ["/t"])})
        lab.adapter = "/nfc0"
        lab.set_status = lambda title, subtitle="", can_enable=False: statuses.append(
            (title, subtitle, can_enable))
        lab.show_tags = lambda tags: statuses.append(tags)
        lab.refresh_adapter()
        assert statuses[-2][:len(expected)] == expected and statuses[-1] == ["/t"]


def test_connect_without_an_adapter(mod):
    statuses = []
    lab = Lab(mod, sync={("/", "GetAdapters"): ([],)})
    lab.set_status = lambda title, subtitle="", can_enable=False: statuses.append(title)
    lab.connect_nfcd()
    lab.sync[("/", "GetAdapters")] = fakes.gerror("no nfcd")
    lab.connect_nfcd()
    assert statuses == ["No NFC adapter", "NFC is not available"]


def test_confirm_write_asks_first_then_writes_the_tlv(mod, monkeypatch):
    dialogs = []

    class Dialog:
        def __init__(self, heading, body):
            self.body = body
            dialogs.append(self)

        def add_response(self, *a):
            pass

        def set_response_appearance(self, *a):
            pass

        def connect(self, signal, handler):
            self.handler = handler

        def present(self, window):
            pass
    monkeypatch.setattr(mod, "Adw", types.SimpleNamespace(
        AlertDialog=Dialog, ResponseAppearance=types.SimpleNamespace(DESTRUCTIVE=2)))
    lab = Lab(mod)
    lab.props = types.SimpleNamespace(active_window=None)
    lab.write_kind = types.SimpleNamespace(get_selected=lambda: 0)
    lab.write_entry = types.SimpleNamespace(get_text=lambda: " https://plinius.cais.de ")
    lab.confirm_write("/t")
    (dialog,) = dialogs
    assert lab.calls == []
    dialog.handler(dialog, "cancel")
    assert lab.calls == []
    dialog.handler(dialog, "write")
    ((path, iface, method, (offset, data), reply),) = lab.calls
    assert (iface, method, offset) == ("org.sailfishos.nfc.TagType2", "WriteData", 0)
    assert data == mod.type2_tlv(mod.ndef_message("link", "https://plinius.cais.de"))
    lab.done((len(data),), None)
    assert lab.toasts == ["Written"]


def test_confirm_write_refuses_what_does_not_fit(mod):
    lab = Lab(mod)
    lab.write_kind = types.SimpleNamespace(get_selected=lambda: 1)
    lab.write_entry = types.SimpleNamespace(get_text=lambda: "x" * 400)
    lab.confirm_write("/t")
    assert lab.toasts == ["too long for this tag"] and lab.calls == []
    lab.write_entry = types.SimpleNamespace(get_text=lambda: "   ")
    lab.confirm_write("/t")
    assert len(lab.toasts) == 1


def test_show_tag_lists_what_the_tag_tells(mod):
    poll = {"NFCID1": b"\x04\xa2\x3b\x52\x81\x70\x80", "SEL_RES": 0x00, "SENS_RES": b"\x44\x00"}
    lab = Lab(mod, sync={
        ("/t", "GetAll"): (1, True, 1, 2, 4, ["org.sailfishos.nfc.Tag",
                                             "org.sailfishos.nfc.TagType2"], ["/r0"]),
        ("/t", "GetPollParameters"): (poll,),
    })
    rows, shown = [], []
    lab.add_group = lambda title, description=None: title
    lab.info_row = lambda group, title, value: rows.append((title, value))
    lab.show_ndef = lambda records: shown.append(("ndef", records))
    lab.show_type2 = lambda path: shown.append(("type2", path))
    lab.show_isodep = lambda path: shown.append(("isodep", path))
    lab.show_raw = lambda path: shown.append(("raw", path))
    lab.show_tag("/t")
    assert rows == [("Technology", "NFC-A"), ("Protocol", "Type 2"),
                    ("Type", "MIFARE Ultralight / NTAG"), ("UID", "04 A2 3B 52 81 70 80"),
                    ("SAK", "00"), ("ATQA", "44 00"), ("Interfaces", "Tag, TagType2")]
    assert shown == [("ndef", ["/r0"]), ("type2", "/t"), ("raw", "/t")]


def test_show_tag_with_unknown_codes(mod):
    lab = Lab(mod, sync={("/t", "GetAll"): (1, True, 64, 128, 0, [], []),
                         ("/t", "GetPollParameters"): fakes.gerror("no poll")})
    rows = []
    lab.add_group = lambda title, description=None: title
    lab.info_row = lambda group, title, value: rows.append((title, value))
    lab.show_ndef = lab.show_raw = lambda *a: None
    lab.show_tag("/t")
    assert rows == [("Technology", "0x40"), ("Protocol", "0x80"), ("Interfaces", "")]


# ----- the whole window against a fake nfcd -----

class _Kind(type):
    """Class attributes of a widget namespace are widget classes again (Adw.ActionRow,
    Gtk.Align.CENTER ...), made on first use and then the same object."""

    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        sub = _Kind(name, (Widget,), {})
        setattr(cls, name, sub)
        return sub


class Widget(metaclass=_Kind):
    """A widget that keeps what is done to it: properties, children, signal handlers."""

    created = []

    def __init__(self, *args, **props):
        self.args = args
        self.values = dict(props)
        self.children = []
        self.handlers = {}
        self.calls = []
        Widget.created.append(self)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name.startswith("set_"):
            return lambda value, *rest: self.values.__setitem__(name[4:], value)
        if name.startswith("get_"):
            return lambda *a: self.values.get(name[4:])
        if name.startswith(("add", "pack")):
            return lambda child, *rest: self.children.append(child)
        return lambda *a: self.calls.append((name,) + a)

    def connect(self, signal, handler):
        self.handlers.setdefault(signal, []).append(handler)

    def emit(self, signal, *args):
        for handler in self.handlers[signal]:
            handler(self, *args)

    def remove(self, child):
        self.children.remove(child)

    @property
    def kind(self):
        return type(self).__name__


class TextBuffer(Widget):
    def __init__(self, **props):
        super().__init__(**props)
        self.text = ""

    def get_end_iter(self):
        return len(self.text)

    def insert(self, where, text):
        self.text = self.text[:where] + text + self.text[where:]

    def set_text(self, text):
        self.text = text

    def get_bounds(self):
        return 0, len(self.text)

    def get_text(self, start, end, hidden):
        return self.text[start:end]

    def get_insert(self):
        return "insert"

    def place_cursor(self, where):
        self.cursor = where


class NfcBus:
    """The system bus with nfcd on it: answers from a table {(path, method): value or
    GLib.Error} ((path, interface, method) where one path has more), built into a variant of
    the asked reply type; asynchronous calls wait."""

    def __init__(self, table):
        self.table = table
        self.pending = []
        self.subscriptions = []

    def call_sync(self, name, path, iface, method, params, reply, flags, timeout, cancel):
        answer = self.table.get((path, iface, method)) or self.table[(path, method)]
        if isinstance(answer, Exception):
            raise answer
        return GLib.Variant(reply.dup_string(), answer)

    def call(self, name, path, iface, method, params, reply, flags, timeout, cancel, finish):
        # "ay" unpacks as a list of ints; bytes compare more readably
        unpacked = tuple(bytes(v) if isinstance(v, list) else v
                         for v in params.unpack()) if params is not None else None
        self.pending.append(((name, path, method, unpacked), reply.dup_string(), finish))

    def answer(self, value):
        """The oldest waiting call gets value (a GLib.Error fails it); returns that call."""
        call, reply, finish = self.pending.pop(0)
        finish(self, (reply, value))
        return call

    def call_finish(self, result):
        reply, value = result
        if isinstance(value, Exception):
            raise value
        return GLib.Variant(reply, value)

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, handler):
        self.subscriptions.append((sender, iface, member, path, handler))


TAG = "/nfc0/tag0"
UID = b"\x04\xa2\x3b\x52\x81\x70\x80"


def nfcd_table(enabled=True, powered=True, tags=(TAG,)):
    return {
        ("/", "GetAdapters"): (["/nfc0"],),
        ("/nfc0", "GetAll"): (1, enabled, powered, 0, 0, True, list(tags)),
        (TAG, "GetAll"): (1, True, 1, 8, 4, ["org.sailfishos.nfc.Tag",
                                             "org.sailfishos.nfc.TagType2",
                                             "org.sailfishos.nfc.IsoDep"], [TAG + "/ndef0"]),
        (TAG, "GetPollParameters"): ({"NFCID1": GLib.Variant("ay", UID),
                                      "SEL_RES": GLib.Variant("y", 0x20),
                                      "SENS_RES": GLib.Variant("ay", b"\x44\x00")},),
        (TAG + "/ndef0", "GetAll"): (1, 0, 1, ["org.sailfishos.nfc.NDEF"], b"U", b"",
                                     b"\x04plinius.cais.de"),
        (TAG, "GetActivationParameters"): ({"HB": GLib.Variant("ay", b"\x80\x31")},),
        (TAG, "org.sailfishos.nfc.TagType2", "GetAll"): (1, 4, 144, UID),
    }


@pytest.fixture
def window(mod, monkeypatch):
    """NFC Lab activated against nfcd: returns (app, bus, widgets made)."""
    def make(table):
        Widget.created = []
        adw, gtk = _Kind("Adw", (Widget,), {}), _Kind("Gtk", (Widget,), {})
        gtk.TextBuffer = TextBuffer
        monkeypatch.setattr(mod, "Adw", adw)
        monkeypatch.setattr(mod, "Gtk", gtk)
        glib = fakes.FakeGLib()
        monkeypatch.setattr(mod, "GLib", glib)
        bus = NfcBus(table)
        monkeypatch.setattr(mod, "Gio", fakes.Proxy(mod.Gio, bus_get_sync=lambda *a: bus))
        monkeypatch.setattr(mod.time, "strftime", lambda fmt: "12:00:00")
        app = mod.NfcLab()
        app.props = types.SimpleNamespace(active_window=None)
        app.do_activate()
        (win,) = made("ApplicationWindow")
        app.props.active_window = win  # what GTK does on present()
        app.glib = glib
        return app, bus
    return make


def made(kind, **values):
    return [w for w in Widget.created if w.kind == kind
            and all(w.values.get(k) == v for k, v in values.items())]


def rows(group):
    return [(r.values.get("title"), r.values.get("subtitle")) for r in group.children
            if r.kind == "ActionRow"]


def log_lines(app):
    return [line[9:] for line in app.log_buffer.text.splitlines()]


def toasts(app):
    return [t.values["title"] for t in app.toast_overlay.children if t.kind == "Toast"]


def test_window_shows_the_tag_with_all_it_tells(mod, window):
    app, bus = window(nfcd_table())
    (win,) = made("ApplicationWindow")
    assert win.values["title"] == "NFC Lab" and ("present",) in win.calls
    assert app.status_row.values["title"] == "Hold a tag or card to the back of the phone"
    assert app.enable_btn.values["visible"] is False
    groups = app.page.children
    assert [g.values.get("title") for g in groups] == [
        None, "Tag", "NDEF", "Memory (Type 2)", "Card (ISO-DEP)", "Raw Frames"]
    assert rows(groups[1]) == [("Technology", "NFC-A"), ("Protocol", "Type 4A (ISO-DEP)"),
                               ("Type", "MIFARE Ultralight / NTAG"),
                               ("UID", "04 A2 3B 52 81 70 80"), ("SAK", "20"),
                               ("ATQA", "44 00"), ("Interfaces", "Tag, TagType2, IsoDep")]
    assert rows(groups[2]) == [("Link", "https://plinius.cais.de")]
    assert rows(groups[3]) == [("Serial", "04 A2 3B 52 81 70 80"),
                               ("Data area", "144 bytes, blocks of 4")]
    assert rows(groups[4]) == [("Historical bytes", "80 31")]
    assert [r.values["title"] for r in groups[4].children if r.kind == "ButtonRow"] == [
        label for label, _apdu in mod.APDU_PRESETS]
    assert log_lines(app) == ["Adapter /nfc0", f"Tag {TAG}: NFC-A, Type 4A (ISO-DEP)",
                              "UID 04 A2 3B 52 81 70 80", "NDEF Link: https://plinius.cais.de"]
    # The log follows its end
    for sid in app.glib.of_kind("idle"):
        app.glib.fire(sid)
    assert app.log_view.calls[-1] == ("scroll_to_mark", "insert", 0.0, False, 0, 0)
    # Activated again: the window comes to the front, nothing is built twice
    count = len(Widget.created)
    app.do_activate()
    assert len(Widget.created) == count and win.calls.count(("present",)) == 2


def test_window_buttons_talk_to_the_tag(mod, window):
    app, bus = window(nfcd_table())
    memory, card, raw = app.page.children[3:6]
    # Read Memory
    (read_row,) = [r for r in memory.children if r.kind == "ButtonRow"]
    read_row.emit("activated")
    assert bus.answer((list(range(18)),))[2] == "ReadAllData"
    assert log_lines(app)[-2:] == ["← 0000  " + mod.hexs(bytes(range(16))), "← 0010  10 11"]
    read_row.emit("activated")
    bus.answer(GLib.Error("tag gone"))
    assert log_lines(app)[-1] == "← failed: tag gone"
    # An APDU preset and one typed in
    card.children[1].emit("activated")
    name, path, method, params = bus.answer((b"o\x00", 0x90, 0x00))
    assert (path, method) == (TAG, "Transmit")
    assert params == (0x00, 0xA4, 4, 0, bytes.fromhex("325041592E5359532E4444463031"), 256)
    assert log_lines(app)[-2:] == ["← 6F 00  SW 9000", "  o."]
    (apdu_entry,) = [r for r in card.children if r.kind == "EntryRow"]
    apdu_entry.values["text"] = "00 84 00 00 08"
    apdu_entry.emit("apply")
    bus.answer((b"", 0x6A, 0x82))
    assert log_lines(app)[-2:] == ["→ 00 84 00 00 08", "← SW 6A82"]  # nothing to show
    # A raw frame
    (raw_entry,) = raw.children
    raw_entry.values["text"] = "30 00"
    raw_entry.emit("apply")
    assert bus.answer((b"\x04\x11",))[1:] == (TAG, "Transceive", (b"\x30\x00",))
    raw_entry.emit("apply")
    bus.answer(GLib.Error("no answer"))
    assert log_lines(app)[-3:] == ["← 04 11", "→ 30 00", "← failed: no answer"]
    raw_entry.values["text"] = "3"
    raw_entry.emit("apply")
    assert bus.pending == [] and toasts(app) == ["Not hex: odd number of hex digits"]


def test_window_writes_a_link_after_asking(mod, window):
    app, bus = window(nfcd_table())
    app.write_kind.values["selected"] = 0
    app.write_entry.values["text"] = "https://example.org"
    app.write_entry.emit("apply")
    (dialog,) = made("AlertDialog")
    assert dialog.values["heading"] == "Write to the Tag?"
    assert ("present", app.props.active_window) in dialog.calls
    dialog.emit("response", "write")
    data = mod.type2_tlv(mod.ndef_message("link", "https://example.org"))
    assert bus.answer(GLib.Error("tag gone"))[2:] == ("WriteData", (0, data))
    assert toasts(app) == ["Writing failed"]
    assert log_lines(app)[-1] == "← failed: tag gone"


def test_window_tags_coming_and_going(mod, window):
    app, bus = window(nfcd_table())
    adapter, settings = bus.subscriptions
    assert adapter[:4] == ("org.sailfishos.nfc.daemon", "org.sailfishos.nfc.Adapter", None, "/nfc0")
    assert settings[:4] == ("org.sailfishos.nfc.settings", "org.sailfishos.nfc.Settings",
                            "EnabledChanged", "/")
    on_signal = adapter[4]
    on_signal(bus, ":1.3", "/nfc0", "org.sailfishos.nfc.Adapter", "TagsChanged",
              GLib.Variant("(ao)", ([],)))
    assert [g.values.get("title") for g in app.page.children] == [None]
    assert log_lines(app)[-1] == "Tag gone" and app.tag_path is None
    # Switched off elsewhere: the button to switch it on appears
    bus.table[("/nfc0", "GetAll")] = (1, False, False, 0, 0, True, [])
    settings[4](bus, ":1.4", "/", "org.sailfishos.nfc.Settings", "EnabledChanged",
                GLib.Variant("(b)", (False,)))
    assert app.status_row.values["title"] == "NFC is off"
    assert app.enable_btn.values["visible"] is True
    app.enable_btn.emit("clicked")
    name, path, method, params = bus.answer(GLib.Error("not allowed"))
    assert (name, path, method, params) == ("org.sailfishos.nfc.settings", "/", "SetEnabled",
                                            (True,))
    assert toasts(app) == ["Switching NFC on failed"]
    assert log_lines(app)[-1] == "SetEnabled failed: not allowed"
    app.enable_btn.emit("clicked")
    bus.answer(())  # switched on: nfcd's signal brings the new state
    assert toasts(app) == ["Switching NFC on failed"]
    bus.table[("/nfc0", "GetAll")] = (1, True, False, 0, 0, True, [])
    on_signal(bus, ":1.3", "/nfc0", "org.sailfishos.nfc.Adapter", "PoweredChanged",
              GLib.Variant("(b)", (False,)))
    assert (app.status_row.values["title"], app.status_row.values["subtitle"]) == (
        "NFC is on", "It looks for tags while the screen is on")
    on_signal(bus, ":1.3", "/nfc0", "org.sailfishos.nfc.Adapter", "ModeChanged",
              GLib.Variant("(u)", (1,)))
    # nfcd going away while the adapter is asked
    bus.table[("/nfc0", "GetAll")] = GLib.Error("nfcd is gone")
    on_signal(bus, ":1.3", "/nfc0", "org.sailfishos.nfc.Adapter", "EnabledChanged",
              GLib.Variant("(b)", (False,)))
    assert (app.status_row.values["title"], app.status_row.values["subtitle"]) == (
        "NFC is not available", "nfcd is gone")
    assert app.enable_btn.values["visible"] is False


@pytest.mark.parametrize("activation", [GLib.Error("not activated"), ({},)])
def test_window_a_tag_that_tells_little(mod, window, activation):
    table = nfcd_table()
    table[(TAG, "GetAll")] = (1, True, 1, 2, 0, ["org.sailfishos.nfc.Tag",
                                                 "org.sailfishos.nfc.TagType2",
                                                 "org.sailfishos.nfc.IsoDep"], [])
    table[(TAG, "GetPollParameters")] = ({},)
    table[(TAG, "GetActivationParameters")] = activation
    app, bus = window(table)
    groups = app.page.children
    assert [g.values.get("title") for g in groups] == [
        None, "Tag", "NDEF", "Memory (Type 2)", "Card (ISO-DEP)", "Raw Frames"]
    # No type, no UID: only what is known
    assert rows(groups[1]) == [("Technology", "NFC-A"), ("Protocol", "Type 2"),
                               ("Interfaces", "Tag, TagType2, IsoDep")]
    assert groups[2].values["description"] == "No NDEF data on this tag"
    assert rows(groups[4]) == []  # no historical bytes


def test_window_type2_info_unreadable_and_a_tag_gone_too_soon(mod, window):
    app, bus = window(nfcd_table())
    bus.table = {(path, method): GLib.Error("tag gone") for path, method in (
        (TAG, "GetAll"), ("/nfc0/tag1", "GetAll"))}
    group_count = len(app.page.children)
    mod.NfcLab.show_type2(app, TAG)
    assert log_lines(app)[-1] == "Type 2 info failed: tag gone"
    assert rows(app.page.children[group_count]) == []
    mod.NfcLab.show_tag(app, "/nfc0/tag1")
    assert log_lines(app)[-1] == "Reading the tag failed: tag gone"


def test_window_copy_and_clear_the_log(mod, window):
    app, bus = window(nfcd_table())
    copy_btn, clear_btn = [b for b in made("Button") if "tooltip_text" in b.values]
    assert copy_btn.values["tooltip_text"] == "Copy the log"
    copied = []
    app.props.active_window.values["clipboard"] = types.SimpleNamespace(set=copied.append)
    copy_btn.emit("clicked")
    assert copied == [app.log_buffer.text] and "Adapter /nfc0" in copied[0]
    assert toasts(app) == ["Log copied"]
    clear_btn.emit("clicked")
    assert app.log_buffer.text == ""


def test_program_runs_the_application(mod):
    runs = []

    class Application:
        def __init__(self, application_id):
            self.application_id = application_id

        def run(self, argv):
            runs.append((self.application_id, list(argv)))
            return 3
    adw = types.SimpleNamespace(Application=Application)
    code = run_as_main(NFCLAB, ["--verbose"], Adw=adw, Gtk=fakes.namespace("Gtk"))
    assert code == 3 and runs == [("org.plinius.NfcLab", [str(NFCLAB), "--verbose"])]


def test_call_without_an_answer_wanted(mod):
    lab = mod.NfcLab.__new__(mod.NfcLab)
    lab.bus = NfcBus({})
    mod.NfcLab.call(lab, "/nfc0", "org.sailfishos.nfc.Adapter", "Ping")
    mod.NfcLab.call(lab, "/nfc0", "org.sailfishos.nfc.Adapter", "Ping")
    assert lab.bus.answer(()) == ("org.sailfishos.nfc.daemon", "/nfc0", "Ping", None)
    lab.bus.answer(GLib.Error("unknown method"))  # dropped: nobody waits for it
    assert lab.bus.pending == []
