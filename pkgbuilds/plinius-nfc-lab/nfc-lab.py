#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""NFC Lab: look at NFC tags and cards held to the phone, and talk to them.

Uses nfcd's D-Bus API (org.sailfishos.nfc.*): the adapter reports tags as they come and go,
each tag tells its technology, protocol and NDEF records, Type 2 tags (NTAG, Ultralight) can be
read and written, ISO-DEP cards (payment cards, ID cards) take APDUs, and every tag takes raw
frames. Everything sent and received is written to the log at the bottom.
"""

import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

APP_ID = "org.plinius.NfcLab"

NFCD = "org.sailfishos.nfc.daemon"
NFC_SETTINGS = "org.sailfishos.nfc.settings"
ADAPTER_IFACE = "org.sailfishos.nfc.Adapter"
TAG_IFACE = "org.sailfishos.nfc.Tag"
TYPE2_IFACE = "org.sailfishos.nfc.TagType2"
ISODEP_IFACE = "org.sailfishos.nfc.IsoDep"
NDEF_IFACE = "org.sailfishos.nfc.NDEF"
DAEMON_IFACE = "org.sailfishos.nfc.Daemon"
SETTINGS_IFACE = "org.sailfishos.nfc.Settings"

TECHNOLOGIES = {1: "NFC-A", 2: "NFC-B", 4: "NFC-F"}
PROTOCOLS = {1: "Type 1", 2: "Type 2", 4: "Type 3 (FeliCa)", 8: "Type 4A (ISO-DEP)",
             16: "Type 4B (ISO-DEP)", 32: "NFC-DEP (peer)"}
TAG_TYPES = {1: "FeliCa", 2: "MIFARE Classic", 4: "MIFARE Ultralight / NTAG"}

# NDEF URI prefixes (NFC Forum URI RTD), index = first payload byte
URI_PREFIXES = ["", "http://www.", "https://www.", "http://", "https://", "tel:", "mailto:",
                "ftp://anonymous:anonymous@", "ftp://ftp.", "ftps://", "sftp://", "smb://",
                "nfs://", "ftp://", "dav://", "news:", "telnet://", "imap:", "rtsp://", "urn:",
                "pop:", "sip:", "sips:", "tftp:", "btspp://", "btl2cap://", "btgoep://",
                "tcpobex://", "irdaobex://", "file://", "urn:epc:id:", "urn:epc:tag:",
                "urn:epc:pat:", "urn:epc:raw:", "urn:epc:", "urn:nfc:"]

# APDU presets for ISO-DEP cards: (label, hex)
APDU_PRESETS = [
    ("Select payment directory (PPSE)", "00A404000E325041592E5359532E444446303100"),
    ("Select NDEF application", "00A4040007D276000085010100"),
    ("Get challenge (8 bytes)", "0084000008"),
    ("Select master file", "00A4000C023F00"),
]


def hexs(data):
    return " ".join(f"{b:02X}" for b in data)


def parse_hex(text):
    clean = "".join(ch for ch in text if ch in "0123456789abcdefABCDEF")
    if len(clean) % 2:
        raise ValueError("odd number of hex digits")
    return bytes.fromhex(clean)


def decode_ndef(tnf, rtype, payload):
    """A readable line for one NDEF record."""
    if tnf == 1 and rtype == b"U" and payload:
        prefix = URI_PREFIXES[payload[0]] if payload[0] < len(URI_PREFIXES) else ""
        return "Link", prefix + payload[1:].decode("utf-8", "replace")
    if tnf == 1 and rtype == b"T" and payload:
        lang_len = payload[0] & 0x3F
        lang = payload[1:1 + lang_len].decode("ascii", "replace")
        text = payload[1 + lang_len:]
        encoding = "utf-8"
        if payload[0] & 0x80:
            # NFC Forum Text RTD: UTF-16 without a byte order mark is big-endian
            encoding = "utf-16" if text[:2] in (b"\xfe\xff", b"\xff\xfe") else "utf-16-be"
        return f"Text ({lang})", text.decode(encoding, "replace")
    if tnf == 2:
        return f"MIME {rtype.decode('ascii', 'replace')}", payload.decode("utf-8", "replace")
    if tnf == 4:
        return f"External {rtype.decode('ascii', 'replace')}", hexs(payload)
    return f"Record (TNF {tnf}, type {rtype.decode('ascii', 'replace')})", hexs(payload)


def ndef_message(kind, value):
    """A single-record NDEF message for a link or a text, as bytes."""
    if kind == "link":
        prefix = 0
        for i, p in enumerate(URI_PREFIXES[1:], 1):
            if value.startswith(p) and len(p) > len(URI_PREFIXES[prefix]):
                prefix = i
        rtype, payload = b"U", bytes([prefix]) + value[len(URI_PREFIXES[prefix]):].encode()
    else:
        rtype, payload = b"T", b"\x02en" + value.encode()
    if len(payload) > 255:
        raise ValueError("too long for this tag")
    # MB, ME, SR, TNF 1 (well-known)
    return bytes([0xD1, len(rtype), len(payload)]) + rtype + payload


def type2_tlv(message):
    """NDEF message TLV plus terminator, as a Type 2 tag stores it."""
    if len(message) < 0xFF:
        return bytes([0x03, len(message)]) + message + b"\xFE"
    return bytes([0x03, 0xFF, len(message) >> 8, len(message) & 0xFF]) + message + b"\xFE"


class NfcLab(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.bus = None
        self.adapter = None
        self.tag_path = None

    # ----- D-Bus -----

    def call(self, path, iface, method, params=None, reply="()", name=NFCD, done=None):
        """Asynchronous call; done(result_or_None, error_or_None)."""
        def finish(conn, res):
            try:
                result = conn.call_finish(res)
            except GLib.Error as e:
                if done:
                    done(None, e)
                return
            if done:
                done(result.unpack(), None)
        self.bus.call(name, path, iface, method, params, GLib.VariantType(reply),
                      Gio.DBusCallFlags.NONE, 10000, None, finish)

    def call_sync(self, path, iface, method, params=None, reply="()", name=NFCD):
        return self.bus.call_sync(name, path, iface, method, params, GLib.VariantType(reply),
                                  Gio.DBusCallFlags.NONE, 5000, None).unpack()

    # ----- Log -----

    def log(self, text, direction=""):
        stamp = time.strftime("%H:%M:%S")
        end = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, f"{stamp} {direction}{' ' if direction else ''}{text}\n")
        GLib.idle_add(lambda: self.log_view.scroll_to_mark(
            self.log_buffer.get_insert(), 0.0, False, 0, 0) and False)
        self.log_buffer.place_cursor(self.log_buffer.get_end_iter())

    # ----- UI -----

    def do_activate(self):
        if self.props.active_window:
            self.props.active_window.present()
            return
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

        win = Adw.ApplicationWindow(application=self, title="NFC Lab", default_width=360,
                                    default_height=720)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy the log")
        copy_btn.connect("clicked", self.on_copy_log)
        header.pack_end(copy_btn)
        clear_btn = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text="Clear the log")
        clear_btn.connect("clicked", lambda *_: self.log_buffer.set_text(""))
        header.pack_end(clear_btn)
        toolbar.add_top_bar(header)

        self.toast_overlay = Adw.ToastOverlay()
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, wide_handle=True,
                          shrink_end_child=False, resize_end_child=False)
        self.page = Adw.PreferencesPage()
        paned.set_start_child(self.page)

        self.log_buffer = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buffer, editable=False, monospace=True,
                                     wrap_mode=Gtk.WrapMode.CHAR, left_margin=8, right_margin=8,
                                     top_margin=6, bottom_margin=6)
        log_scroll = Gtk.ScrolledWindow(child=self.log_view, min_content_height=180)
        paned.set_end_child(log_scroll)

        self.toast_overlay.set_child(paned)
        toolbar.set_content(self.toast_overlay)
        win.set_content(toolbar)

        self.build_status_group()
        self.tag_groups = []
        win.present()

        self.connect_nfcd()

    def toast(self, text):
        self.toast_overlay.add_toast(Adw.Toast(title=text))

    def build_status_group(self):
        self.status_group = Adw.PreferencesGroup()
        self.status_row = Adw.ActionRow(title="Looking for nfcd…")
        self.status_icon = Gtk.Image(icon_name=APP_ID + "-symbolic", pixel_size=32)
        self.status_row.add_prefix(self.status_icon)
        self.enable_btn = Gtk.Button(label="Switch On", valign=Gtk.Align.CENTER, visible=False)
        self.enable_btn.add_css_class("suggested-action")
        self.enable_btn.connect("clicked", self.on_enable)
        self.status_row.add_suffix(self.enable_btn)
        self.status_group.add(self.status_row)
        self.page.add(self.status_group)

    def set_status(self, title, subtitle="", can_enable=False):
        self.status_row.set_title(title)
        self.status_row.set_subtitle(subtitle)
        self.enable_btn.set_visible(can_enable)

    # ----- nfcd -----

    def connect_nfcd(self):
        try:
            adapters = self.call_sync("/", DAEMON_IFACE, "GetAdapters", reply="(ao)")[0]
        except GLib.Error as e:
            self.set_status("NFC is not available", e.message)
            return
        if not adapters:
            self.set_status("No NFC adapter")
            return
        self.adapter = adapters[0]
        self.bus.signal_subscribe(NFCD, ADAPTER_IFACE, None, self.adapter, None,
                                  Gio.DBusSignalFlags.NONE, self.on_adapter_signal)
        self.bus.signal_subscribe(NFC_SETTINGS, SETTINGS_IFACE, "EnabledChanged", "/", None,
                                  Gio.DBusSignalFlags.NONE, lambda *_: self.refresh_adapter())
        self.log(f"Adapter {self.adapter}")
        self.refresh_adapter()

    def refresh_adapter(self):
        try:
            (_v, enabled, powered, _modes, _mode, _present,
             tags) = self.call_sync(self.adapter, ADAPTER_IFACE, "GetAll", reply="(ibbuubao)")
        except GLib.Error as e:
            self.set_status("NFC is not available", e.message)
            return
        if not enabled:
            self.set_status("NFC is off", can_enable=True)
        elif not powered:
            self.set_status("NFC is on", "It looks for tags while the screen is on")
        else:
            self.set_status("Hold a tag or card to the back of the phone",
                            "NFC is on and looking")
        self.show_tags(tags)

    def on_adapter_signal(self, _conn, _sender, _path, _iface, signal, params):
        if signal == "TagsChanged":
            self.show_tags(params.unpack()[0])
        elif signal in ("EnabledChanged", "PoweredChanged"):
            self.refresh_adapter()

    def on_enable(self, _btn):
        def done(_res, err):
            if err:
                self.toast("Switching NFC on failed")
                self.log(f"SetEnabled failed: {err.message}")
        self.call("/", SETTINGS_IFACE, "SetEnabled", GLib.Variant("(b)", (True,)),
                  name=NFC_SETTINGS, done=done)

    # ----- Tags -----

    def clear_tag_groups(self):
        for group in self.tag_groups:
            self.page.remove(group)
        self.tag_groups = []

    def add_group(self, title, description=None):
        group = Adw.PreferencesGroup(title=title)
        if description:
            group.set_description(description)
        self.page.add(group)
        self.tag_groups.append(group)
        return group

    def info_row(self, group, title, value):
        row = Adw.ActionRow(title=title, subtitle=value, subtitle_selectable=True)
        row.add_css_class("property")
        group.add(row)

    def show_tags(self, tags):
        if self.tag_path and self.tag_path in tags:
            return
        if self.tag_path:
            self.log("Tag gone")
        self.clear_tag_groups()
        self.tag_path = tags[0] if tags else None
        if self.tag_path:
            self.show_tag(self.tag_path)

    def show_tag(self, path):
        try:
            (_v, _present, tech, proto, ttype, ifaces,
             records) = self.call_sync(path, TAG_IFACE, "GetAll", reply="(ibuuuasao)")
        except GLib.Error as e:
            self.log(f"Reading the tag failed: {e.message}")
            return
        poll = {}
        try:
            poll = self.call_sync(path, TAG_IFACE, "GetPollParameters", reply="(a{sv})")[0]
        except GLib.Error:
            pass

        self.log(f"Tag {path}: {TECHNOLOGIES.get(tech, tech)}, {PROTOCOLS.get(proto, proto)}")
        group = self.add_group("Tag")
        self.info_row(group, "Technology", TECHNOLOGIES.get(tech, f"0x{tech:02X}"))
        self.info_row(group, "Protocol", PROTOCOLS.get(proto, f"0x{proto:02X}"))
        if ttype:
            self.info_row(group, "Type", TAG_TYPES.get(ttype, f"0x{ttype:02X}"))
        for key in ("NFCID1", "NFCID0", "NFCID2"):
            if key in poll:
                self.info_row(group, "UID" if key != "NFCID2" else "IDm", hexs(poll[key]))
                self.log(f"UID {hexs(poll[key])}")
        if "SEL_RES" in poll:
            self.info_row(group, "SAK", f"{poll['SEL_RES']:02X}")
        if "SENS_RES" in poll:
            self.info_row(group, "ATQA", hexs(poll["SENS_RES"]))
        short = [i.rsplit(".", 1)[-1] for i in ifaces]
        self.info_row(group, "Interfaces", ", ".join(short))

        self.show_ndef(records)
        if TYPE2_IFACE in ifaces:
            self.show_type2(path)
        if ISODEP_IFACE in ifaces:
            self.show_isodep(path)
        self.show_raw(path)

    def show_ndef(self, records):
        group = self.add_group("NDEF", None if records else "No NDEF data on this tag")
        for rec in records:
            try:
                (_v, _flags, tnf, _ifaces, rtype, _rid,
                 payload) = self.call_sync(rec, NDEF_IFACE, "GetAll", reply="(iuuasayayay)")
            except GLib.Error as e:
                self.log(f"Reading {rec} failed: {e.message}")
                continue
            title, value = decode_ndef(tnf, bytes(rtype), bytes(payload))
            self.info_row(group, title, value)
            self.log(f"NDEF {title}: {value}")

    # ----- Type 2 tags: read memory, write NDEF -----

    def show_type2(self, path):
        group = self.add_group("Memory (Type 2)")
        try:
            (_v, block, size, serial) = self.call_sync(path, TYPE2_IFACE, "GetAll",
                                                       reply="(iuuay)")
            self.info_row(group, "Serial", hexs(serial))
            self.info_row(group, "Data area", f"{size} bytes, blocks of {block}")
        except GLib.Error as e:
            self.log(f"Type 2 info failed: {e.message}")

        read_row = Adw.ButtonRow(title="Read Memory")
        read_row.connect("activated", lambda *_: self.read_type2(path))
        group.add(read_row)

        self.write_kind = Adw.ComboRow(title="Write", model=Gtk.StringList.new(["Link", "Text"]))
        group.add(self.write_kind)
        self.write_entry = Adw.EntryRow(title="https://… or text", show_apply_button=True)
        self.write_entry.connect("apply", lambda *_: self.confirm_write(path))
        group.add(self.write_entry)

    def read_type2(self, path):
        self.log("ReadAllData", "→")

        def done(res, err):
            if err:
                self.log(f"failed: {err.message}", "←")
                return
            data = bytes(res[0])
            for off in range(0, len(data), 16):
                self.log(f"{off:04X}  {hexs(data[off:off + 16])}", "←")
        self.call(path, TYPE2_IFACE, "ReadAllData", reply="(ay)", done=done)

    def confirm_write(self, path):
        kind = "link" if self.write_kind.get_selected() == 0 else "text"
        value = self.write_entry.get_text().strip()
        if not value:
            return
        try:
            data = type2_tlv(ndef_message(kind, value))
        except ValueError as e:
            self.toast(str(e))
            return
        dialog = Adw.AlertDialog(heading="Write to the Tag?",
                                 body=f"The tag's NDEF data is replaced by this {kind}:\n{value}")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("write", "Write")
        dialog.set_response_appearance("write", Adw.ResponseAppearance.DESTRUCTIVE)

        def response(_d, answer):
            if answer != "write":
                return
            self.log(f"WriteData 0 {hexs(data)}", "→")

            def done(res, err):
                if err:
                    self.log(f"failed: {err.message}", "←")
                    self.toast("Writing failed")
                else:
                    self.log(f"{res[0]} bytes written", "←")
                    self.toast("Written")
            self.call(path, TYPE2_IFACE, "WriteData",
                      GLib.Variant("(uay)", (0, data)), reply="(u)", done=done)
        dialog.connect("response", response)
        dialog.present(self.props.active_window)

    # ----- ISO-DEP cards: APDUs -----

    def show_isodep(self, path):
        group = self.add_group("Card (ISO-DEP)", "Commands (APDUs) to the card's applications")
        try:
            params = self.call_sync(path, ISODEP_IFACE, "GetActivationParameters",
                                    reply="(a{sv})")[0]
            if "HB" in params:
                self.info_row(group, "Historical bytes", hexs(params["HB"]))
        except GLib.Error:
            pass
        for label, apdu in APDU_PRESETS:
            row = Adw.ButtonRow(title=label)
            row.connect("activated", lambda _r, a=apdu: self.send_apdu(path, parse_hex(a)))
            group.add(row)
        entry = Adw.EntryRow(title="APDU (hex), e.g. 00 A4 04 00 …", show_apply_button=True)
        entry.connect("apply", lambda e: self.send_apdu_text(path, e.get_text()))
        group.add(entry)

    def send_apdu_text(self, path, text):
        try:
            self.send_apdu(path, parse_hex(text))
        except ValueError as e:
            self.toast(f"Not an APDU: {e}")

    def send_apdu(self, path, apdu):
        if len(apdu) < 4:
            self.toast("An APDU has at least 4 bytes")
            return
        cla, ins, p1, p2 = apdu[:4]
        body = apdu[4:]
        data, le = b"", 0
        # Case 2: Le only; case 3/4: Lc, data and an optional Le
        if len(body) == 1:
            le = body[0] or 256
        elif len(body) > 1:
            lc = body[0]
            data = body[1:1 + lc]
            if len(body) > 1 + lc:
                le = body[1 + lc] or 256
        self.log(hexs(apdu), "→")

        def done(res, err):
            if err:
                self.log(f"failed: {err.message}", "←")
                return
            resp, sw1, sw2 = bytes(res[0]), res[1], res[2]
            self.log(f"{hexs(resp)}  SW {sw1:02X}{sw2:02X}".strip(), "←")
            printable = "".join(chr(b) if 32 <= b < 127 else "." for b in resp)
            if resp:
                self.log(f"  {printable}")
        self.call(path, ISODEP_IFACE, "Transmit",
                  GLib.Variant("(yyyyayu)", (cla, ins, p1, p2, data, le)),
                  reply="(ayyy)", done=done)

    # ----- Any tag: raw frames -----

    def show_raw(self, path):
        group = self.add_group("Raw Frames", "Send bytes as they are, e.g. 30 00 reads "
                                             "4 blocks of a Type 2 tag")
        entry = Adw.EntryRow(title="Frame (hex)", show_apply_button=True)
        entry.connect("apply", lambda e: self.send_raw(path, e.get_text()))
        group.add(entry)

    def send_raw(self, path, text):
        try:
            frame = parse_hex(text)
        except ValueError as e:
            self.toast(f"Not hex: {e}")
            return
        self.log(hexs(frame), "→")

        def done(res, err):
            if err:
                self.log(f"failed: {err.message}", "←")
            else:
                self.log(hexs(bytes(res[0])), "←")
        self.call(path, TAG_IFACE, "Transceive", GLib.Variant("(ay)", (frame,)),
                  reply="(ay)", done=done)

    # ----- Log actions -----

    def on_copy_log(self, _btn):
        start, end = self.log_buffer.get_bounds()
        self.props.active_window.get_clipboard().set(self.log_buffer.get_text(start, end, False))
        self.toast("Log copied")


if __name__ == "__main__":
    sys.exit(NfcLab().run(sys.argv))
