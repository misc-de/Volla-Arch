#!/usr/bin/python3
"""plinius-cardshare: give the own contact card to another phone, and take one in.

A session D-Bus service (org.plinius.CardShare) behind the quick setting "Contact Card" in
Phosh and the page "Contact Card" in GNOME Settings. It is the only part that talks to NFC,
Bluetooth and the address books; the tile shows its state and draws the QR code.

Share: for a limited time (30 s by default) the phone emulates an NFC Type 4 tag holding the
card (nfcd, through ndef-share), and the tile shows the same card as a QR code. A phone that
read the tag ends the sharing. Receive: for the same time the phone looks for NFC tags with a
card and takes a card pushed over Bluetooth (OBEX Object Push through obexd), while it is
visible as a Bluetooth device. Received cards go into the address book only when asked.

NFC and Bluetooth visibility go back to what they were afterwards. Nothing starts while the
phone is locked, and locking it stops what runs. Which contact is the own card and which of
its fields go out is set in GNOME Settings › NFC › Contact Card (org.plinius.cardshare).
"""

import gettext
import os
import signal
import subprocess
import sys
import tempfile

import gi

gi.require_version("EDataServer", "1.2")
gi.require_version("EBook", "1.2")
gi.require_version("EBookContacts", "1.2")
gi.require_version("GLibUnix", "2.0")
from gi.repository import EBook, EBookContacts, EDataServer, Gio, GLib, GLibUnix  # noqa: E402

_ = gettext.translation("plinius-cardshare", fallback=True).gettext

BUS_NAME = "org.plinius.CardShare"
OBJECT_PATH = "/org/plinius/CardShare"
AGENT_PATH = "/org/plinius/CardShare/ObexAgent"

NFC_DAEMON = "org.sailfishos.nfc.daemon"
NFC_SETTINGS = "org.sailfishos.nfc.settings"
NFC_ADAPTER = "/nfc0"
NFC_ADAPTER_IFACE = "org.sailfishos.nfc.Adapter"
NFC_TAG_IFACE = "org.sailfishos.nfc.Tag"
NFC_NDEF_IFACE = "org.sailfishos.nfc.NDEF"
NEARD = "org.neard"

BLUEZ = "org.bluez"
BLUEZ_ADAPTER = "/org/bluez/hci0"
BLUEZ_ADAPTER_IFACE = "org.bluez.Adapter1"
OBEX = "org.bluez.obex"
OBEX_TRANSFER_IFACE = "org.bluez.obex.Transfer1"

# The longest a session may run with "+30 s", and how long the service stays without one
MAX_SECONDS = 300
# What "+30 s" adds
EXTEND_SECONDS = 30
IDLE_EXIT_SECONDS = 120
# Cards are small; anything larger is not a contact card
MAX_CARD_BYTES = 256 * 1024
# A QR code with more than this is hard to scan from a phone screen: fields get dropped
MAX_QR_BYTES = 900
VCARD_TYPES = ("text/vcard", "text/x-vcard", "text/directory")

# Field names of org.plinius.cardshare.fields and the vCard properties each one stands for
FIELD_PROPERTIES = {
    "name": ("FN", "N"),
    "phone": ("TEL",),
    "email": ("EMAIL",),
    "organization": ("ORG", "TITLE"),
    "address": ("ADR",),
    "website": ("URL",),
    "birthday": ("BDAY",),
    "note": ("NOTE",),
    "photo": ("PHOTO",),
}
# Dropped first when the QR code would get too large (the photo never goes into it)
QR_DROP_ORDER = ("NOTE", "ADR", "URL", "BDAY", "TITLE", "ORG")

INTERFACE_XML = """
<node>
  <interface name="org.plinius.CardShare">
    <!-- idle, sharing or receiving -->
    <property name="State" type="s" access="read"/>
    <!-- Seconds left, and the length of the session for a progress ring -->
    <property name="Remaining" type="u" access="read"/>
    <property name="Duration" type="u" access="read"/>
    <!-- The own card while sharing, as the QR code shows it -->
    <property name="CardText" type="s" access="read"/>
    <!-- Name on the own card, empty when none is chosen -->
    <property name="CardName" type="s" access="read"/>
    <!-- Ways that are on in this session: nfc, qr, bluetooth -->
    <property name="Channels" type="as" access="read"/>
    <!-- Name the phone has for Bluetooth, empty when Bluetooth is off -->
    <property name="BluetoothName" type="s" access="read"/>
    <!-- Cards that came in: id, name, a line with number or e-mail -->
    <property name="Received" type="a(sss)" access="read"/>
    <property name="Error" type="s" access="read"/>
    <method name="Share"/>
    <method name="Receive"/>
    <method name="Extend"/>
    <method name="Stop"/>
    <!-- Adds a received card to the default address book, returns its name -->
    <method name="Import">
      <arg direction="in" name="id" type="s"/>
      <arg direction="out" name="name" type="s"/>
    </method>
    <method name="Dismiss">
      <arg direction="in" name="id" type="s"/>
    </method>
    <!-- For GNOME Settings: contacts to choose from (book uid, contact uid, name) -->
    <method name="ListContacts">
      <arg direction="out" name="contacts" type="a(sss)"/>
    </method>
    <!-- For GNOME Settings: the own card as it would go out -->
    <method name="PreviewCard">
      <arg direction="in" name="for_qr" type="b"/>
      <arg direction="out" name="vcard" type="s"/>
    </method>
    <!-- A phone read the card over NFC -->
    <signal name="CardRead">
      <arg name="channel" type="s"/>
    </signal>
  </interface>
</node>
"""

AGENT_XML = """
<node>
  <interface name="org.bluez.obex.Agent1">
    <method name="Release"/>
    <method name="AuthorizePush">
      <arg direction="in" name="transfer" type="o"/>
      <arg direction="out" name="path" type="s"/>
    </method>
    <method name="Cancel"/>
  </interface>
</node>
"""


class CardError(Exception):
    """An error with a message for the user, returned over D-Bus."""


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def card_summary(text):
    """Name and one line of detail (number or e-mail) of a vCard, for lists and notifications."""
    card = EBookContacts.VCard.new_from_string(text)
    name = ""
    detail = ""
    for prop in ("FN", "N"):
        attr = card.get_attribute(prop)
        if attr:
            values = list(attr.get_values() or [])
            if prop == "N":
                # Family;Given;Additional;Prefix;Suffix → "Given Family"; the positions count
                # before empty ones are dropped (N:;John;Quincy;; has no family name)
                values = values[1:2] + values[0:1]
            name = " ".join(v for v in values if v).strip()
        if name:
            break
    for prop in ("TEL", "EMAIL"):
        attr = card.get_attribute(prop)
        if attr and attr.get_value():
            detail = attr.get_value().strip()
            break
    return name or _("Unnamed contact"), detail


class CardShare:
    def __init__(self, loop):
        self.loop = loop
        self.session = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.system = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        self.settings = Gio.Settings.new("org.plinius.cardshare")
        self.node = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)
        self.agent_node = Gio.DBusNodeInfo.new_for_xml(AGENT_XML)
        self.registry = None
        self.books = {}

        self.state = "idle"
        self.remaining = 0
        self.duration = 0
        self.card_text = ""
        self.channels = []
        self.error = ""
        self.received = []  # [(id, name, detail, vcard text)]
        self.next_id = 1

        self.tick_id = 0
        self.idle_id = 0
        self.share_proc = None
        self.share_watch = 0
        self.workdir = None
        self.nfc_was_enabled = None
        self.tags_sub = 0
        self.seen_tags = set()
        self.bt_was_discoverable = None
        self.bt_timeout_before = None
        self.agent_reg = 0
        self.transfers = {}  # transfer path → (file path, subscription)
        self.pushes = 0  # numbers the received files: one per push, also while others run
        self.inhibit_cookie = 0
        self.notifications = {}  # notification id → card id
        self.screensaver_sub = 0

        self.session.register_object(OBJECT_PATH, self.node.interfaces[0],
                                     self.on_method_call, self.on_get_property, None)
        self.session.signal_subscribe(
            "org.freedesktop.Notifications", "org.freedesktop.Notifications", "ActionInvoked",
            "/org/freedesktop/Notifications", None, Gio.DBusSignalFlags.NONE,
            self.on_notification_action)
        self.session.signal_subscribe(
            "org.freedesktop.Notifications", "org.freedesktop.Notifications",
            "NotificationClosed", "/org/freedesktop/Notifications", None,
            Gio.DBusSignalFlags.NONE, self.on_notification_closed)
        # Locking the phone stops what runs (phosh owns org.gnome.ScreenSaver)
        self.screensaver_sub = self.session.signal_subscribe(
            "org.gnome.ScreenSaver", "org.gnome.ScreenSaver", "ActiveChanged",
            "/org/gnome/ScreenSaver", None, Gio.DBusSignalFlags.NONE, self.on_lock_changed)
        self.settings.connect("changed", self.on_settings_changed)
        self.schedule_idle_exit()

    # ----- D-Bus plumbing -----

    def on_get_property(self, conn, sender, path, iface, name):
        if name == "State":
            return GLib.Variant("s", self.state)
        if name == "Remaining":
            return GLib.Variant("u", self.remaining)
        if name == "Duration":
            return GLib.Variant("u", self.duration)
        if name == "CardText":
            return GLib.Variant("s", self.card_text)
        if name == "CardName":
            return GLib.Variant("s", self.own_card_name())
        if name == "Channels":
            return GLib.Variant("as", self.channels)
        if name == "BluetoothName":
            return GLib.Variant("s", self.bluetooth_name())
        if name == "Received":
            return GLib.Variant("a(sss)", [(i, n, d) for i, n, d, _t in self.received])
        if name == "Error":
            return GLib.Variant("s", self.error)
        return None

    def changed(self, *names):
        values = {n: self.on_get_property(None, None, None, None, n) for n in names}
        self.session.emit_signal(None, OBJECT_PATH, "org.freedesktop.DBus.Properties",
                                 "PropertiesChanged",
                                 GLib.Variant("(sa{sv}as)", (BUS_NAME, values, [])))

    def on_method_call(self, conn, sender, path, iface, method, params, invocation):
        try:
            if method == "Share":
                self.start("sharing")
                invocation.return_value(None)
            elif method == "Receive":
                self.start("receiving")
                invocation.return_value(None)
            elif method == "Extend":
                self.extend()
                invocation.return_value(None)
            elif method == "Stop":
                self.stop()
                invocation.return_value(None)
            elif method == "Import":
                invocation.return_value(GLib.Variant("(s)", (self.import_card(params[0]),)))
            elif method == "Dismiss":
                self.dismiss(params[0])
                invocation.return_value(None)
            elif method == "ListContacts":
                invocation.return_value(GLib.Variant("(a(sss))", (self.list_contacts(),)))
            elif method == "PreviewCard":
                invocation.return_value(GLib.Variant("(s)", (self.own_card(params[0]),)))
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
        except CardError as e:
            invocation.return_dbus_error("org.plinius.CardShare.Error", str(e))
        except GLib.Error as e:
            log(f"{method} failed: {e.message}")
            invocation.return_dbus_error("org.plinius.CardShare.Error", e.message)
        except Exception as e:  # noqa: BLE001 - a caller must never wait for the timeout
            log(f"{method} failed: {type(e).__name__}: {e}")
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(e))
        self.schedule_idle_exit()

    def call(self, bus, name, path, iface, method, args=None, reply=None, timeout=5000):
        return bus.call_sync(name, path, iface, method, args,
                             GLib.VariantType(reply) if reply else None,
                             Gio.DBusCallFlags.NONE, timeout, None)

    def get_prop(self, bus, name, path, iface, prop):
        return self.call(bus, name, path, "org.freedesktop.DBus.Properties", "Get",
                         GLib.Variant("(ss)", (iface, prop)), "(v)")[0]

    def set_prop(self, bus, name, path, iface, prop, value):
        self.call(bus, name, path, "org.freedesktop.DBus.Properties", "Set",
                  GLib.Variant("(ssv)", (iface, prop, value)))

    def schedule_idle_exit(self):
        """Exit when nothing runs and nothing waits to be imported; D-Bus starts it again."""
        if self.idle_id:
            GLib.source_remove(self.idle_id)
            self.idle_id = 0
        if self.state == "idle" and not self.received:
            self.idle_id = GLib.timeout_add_seconds(IDLE_EXIT_SECONDS, self.loop.quit)

    # ----- Address books -----

    def book_client(self, source_uid):
        if source_uid in self.books:
            return self.books[source_uid]
        if not self.registry:
            self.registry = EDataServer.SourceRegistry.new_sync(None)
        source = self.registry.ref_source(source_uid)
        if not source:
            raise CardError(_("The address book of the card is gone"))
        # Without waiting for a remote book to go online (-1; 0 waits forever): its cache
        # has the contacts
        client = EBook.BookClient.connect_sync(source, 0xFFFFFFFF, None)
        self.books[source_uid] = client
        return client

    def list_contacts(self):
        if not self.registry:
            self.registry = EDataServer.SourceRegistry.new_sync(None)
        result = []
        for source in self.registry.list_enabled(EDataServer.SOURCE_EXTENSION_ADDRESS_BOOK):
            try:
                client = self.book_client(source.get_uid())
                ok, contacts = client.get_contacts_sync(
                    '(exists "full_name")', None)
            except GLib.Error as e:
                log(f"Address book {source.get_uid()} not readable: {e.message}")
                continue
            for contact in contacts or []:
                name = contact.get_property("full-name") or ""
                if name:
                    result.append((source.get_uid(), contact.get_property("id"), name))
        result.sort(key=lambda c: c[2].casefold())
        return result

    def own_contact(self):
        book = self.settings.get_string("contact-book")
        uid = self.settings.get_string("contact-uid")
        if not book or not uid:
            raise CardError(_("No card chosen yet. Choose yours in Settings › NFC › Contact Card."))
        try:
            ok, contact = self.book_client(book).get_contact_sync(uid, None)
        except GLib.Error:
            raise CardError(_("The contact of the card is gone. Choose it again in Settings."))
        return contact

    def own_card_name(self):
        """Name on the own card for the tile, empty when none is chosen or it is gone."""
        try:
            return self.own_contact().get_property("full-name") or ""
        except (CardError, GLib.Error):
            return ""

    def own_card(self, for_qr):
        """The own card with only the chosen fields, as vCard 3.0 text."""
        contact = self.own_contact()
        fields = self.settings.get_strv("fields")
        keep = set()
        for field in fields:
            keep.update(FIELD_PROPERTIES.get(field, ()))
        if for_qr:
            keep.discard("PHOTO")
        # FN is required by vCard 3.0, and the other phone shows it
        keep.update(("FN", "N"))

        def build(props):
            # vCard 3.0 as Android and iOS read it; to_string() keeps the card's version
            card = EBookContacts.VCard.new_from_string(
                "BEGIN:VCARD\r\nVERSION:3.0\r\nEND:VCARD\r\n")
            for attr in contact.get_attributes():
                name = attr.get_name().upper()
                if name not in props:
                    continue
                copy = attr.copy()
                # Evolution's own parameters (X-EVOLUTION-UI-SLOT …) mean nothing elsewhere
                for param in list(copy.get_params() or []):
                    if param.get_name().upper().startswith("X-"):
                        copy.remove_param(param.get_name())
                card.append_attribute(copy)
            return card.to_string()

        text = build(keep)
        if for_qr:
            for prop in QR_DROP_ORDER:
                if len(text.encode()) <= MAX_QR_BYTES:
                    break
                if prop in keep:
                    keep.discard(prop)
                    text = build(keep)
        return text

    def import_card(self, card_id):
        entry = next((r for r in self.received if r[0] == card_id), None)
        if not entry:
            raise CardError(_("This card is no longer there"))
        if not self.registry:
            self.registry = EDataServer.SourceRegistry.new_sync(None)
        source = self.registry.ref_default_address_book()
        client = self.book_client(source.get_uid())
        contact = EBookContacts.Contact.new_from_vcard(entry[3])
        # A new contact of its own, not one with the sender's ids
        contact.set_property("id", None)
        for prop in ("UID", "REV", "X-EVOLUTION-FILE-AS"):
            contact.remove_attributes(None, prop)
        client.add_contact_sync(contact, EBook.BookOperationFlags.NONE, None)
        log(f"Card {card_id} imported")
        self.dismiss(card_id)
        return entry[1]

    def dismiss(self, card_id):
        before = len(self.received)
        self.received = [r for r in self.received if r[0] != card_id]
        for nid, cid in list(self.notifications.items()):
            if cid == card_id:
                self.close_notification(nid)
        if len(self.received) != before:
            self.changed("Received")

    def add_received(self, text, channel):
        """A card came in: keep it until imported or dismissed, and tell the user."""
        if "BEGIN:VCARD" not in text.upper():
            log(f"Not a card over {channel}, ignored")
            return
        try:
            name, detail = card_summary(text)
        except GLib.Error:
            log(f"Unreadable card over {channel}, ignored")
            return
        # The same card held twice is one card
        for entry in self.received:
            if entry[3] == text:
                return
        card_id = str(self.next_id)
        self.next_id += 1
        self.received.append((card_id, name, detail, text))
        log(f"Card {card_id} received over {channel}")
        self.changed("Received")
        self.feedback("message-new-instant")
        self.notify_received(card_id, name, detail)

    # ----- Sessions -----

    def is_locked(self):
        try:
            return self.call(self.session, "org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
                             "org.gnome.ScreenSaver", "GetActive", None, "(b)")[0]
        except GLib.Error:
            return False

    def start(self, state):
        if self.is_locked():
            raise CardError(_("Unlock the phone first"))
        if self.state != "idle":
            self.stop()
        card = None
        if state == "sharing":
            # Fails early without a card, before NFC goes on
            card = self.own_card(False)
            qr = self.own_card(True)

        self.state = state
        self.error = ""
        self.duration = self.remaining = max(10, min(self.settings.get_uint("seconds"),
                                                     MAX_SECONDS))
        self.channels = []
        self.workdir = tempfile.mkdtemp(prefix="plinius-cardshare-",
                                        dir=GLib.get_user_runtime_dir())
        self.inhibit_idle()

        problems = []
        if state == "sharing":
            self.card_text = qr
            self.channels.append("qr")
            try:
                self.share_nfc(card)
                self.channels.append("nfc")
            except (GLib.Error, OSError, CardError) as e:
                log(f"NFC sharing failed: {getattr(e, 'message', e)}")
                problems.append(_("NFC is not available, the QR code still works"))
        else:
            try:
                self.receive_nfc()
                self.channels.append("nfc")
            except (GLib.Error, CardError) as e:
                log(f"NFC reading failed: {getattr(e, 'message', e)}")
                problems.append(_("NFC is not available"))
            try:
                self.receive_bluetooth()
                self.channels.append("bluetooth")
            except (GLib.Error, CardError) as e:
                log(f"Bluetooth receiving failed: {getattr(e, 'message', e)}")
                problems.append(str(e) if isinstance(e, CardError)
                                else _("Bluetooth is not available"))
        self.error = "\n".join(problems)
        log(f"Started {state} for {self.duration} s over {', '.join(self.channels) or 'nothing'}")
        self.tick_id = GLib.timeout_add_seconds(1, self.on_tick)
        self.changed("State", "Remaining", "Duration", "CardText", "Channels",
                     "BluetoothName", "Error")

    def extend(self):
        if self.state == "idle":
            return
        add = min(EXTEND_SECONDS, MAX_SECONDS - self.remaining)
        if add <= 0:
            return
        self.remaining += add
        self.duration = max(self.duration, self.remaining)
        if "bluetooth" in self.channels:
            self.set_discoverable_timeout()
        self.changed("Remaining", "Duration")

    def on_tick(self):
        self.remaining = max(0, self.remaining - 1)
        if self.remaining == 0:
            self.tick_id = 0
            self.stop()
            return GLib.SOURCE_REMOVE
        self.changed("Remaining")
        return GLib.SOURCE_CONTINUE

    def stop(self, error=""):
        if self.state == "idle":
            return
        log(f"Stopped {self.state}")
        if self.tick_id:
            GLib.source_remove(self.tick_id)
            self.tick_id = 0
        self.stop_share_nfc()
        self.stop_receive_nfc()
        self.stop_receive_bluetooth()
        self.restore_nfc()
        self.uninhibit_idle()
        if self.workdir:
            for name in os.listdir(self.workdir):
                os.unlink(os.path.join(self.workdir, name))
            os.rmdir(self.workdir)
            self.workdir = None
        self.state = "idle"
        self.remaining = self.duration = 0
        self.card_text = ""
        self.channels = []
        self.error = error
        self.changed("State", "Remaining", "Duration", "CardText", "Channels", "Error")
        self.schedule_idle_exit()

    def on_lock_changed(self, conn, sender, path, iface, signal_name, params):
        if params[0] and self.state != "idle":
            self.stop()

    def on_settings_changed(self, settings, key):
        if key in ("contact-book", "contact-uid"):
            self.changed("CardName")

    # ----- NFC -----

    def enable_nfc(self):
        """NFC on for this session; restore_nfc() puts it back as it was."""
        enabled = self.call(self.system, NFC_SETTINGS, "/", "org.sailfishos.nfc.Settings",
                            "GetEnabled", None, "(b)")[0]
        if self.nfc_was_enabled is None:
            self.nfc_was_enabled = enabled
        if not enabled:
            self.call(self.system, NFC_SETTINGS, "/", "org.sailfishos.nfc.Settings",
                      "SetEnabled", GLib.Variant("(b)", (True,)))
        # The display hook powers the adapter only on a display change
        powered = False
        for _attempt in range(20):
            try:
                self.set_prop(self.system, NEARD, NFC_ADAPTER, "org.neard.Adapter", "Powered",
                              GLib.Variant("b", True))
                powered = True
                break
            except GLib.Error:
                # nfcd brings the adapter back a moment after it is enabled
                GLib.usleep(100000)
        if not powered:
            raise CardError(_("NFC is not available"))
        # Card emulation asked for while the chip is still starting its discovery fails
        # (RF_DISCOVER_RSP error 1, the mode stays 0): wait until it polls
        for _attempt in range(30):
            try:
                if self.call(self.system, NFC_DAEMON, NFC_ADAPTER, NFC_ADAPTER_IFACE, "GetMode",
                             None, "(u)")[0]:
                    return
            except GLib.Error:
                pass
            GLib.usleep(100000)
        log("NFC does not poll yet, going on anyway")

    def restore_nfc(self):
        if self.nfc_was_enabled is False:
            try:
                self.call(self.system, NFC_SETTINGS, "/", "org.sailfishos.nfc.Settings",
                          "SetEnabled", GLib.Variant("(b)", (False,)))
            except GLib.Error as e:
                log(f"Switching NFC off again failed: {e.message}")
        self.nfc_was_enabled = None

    def share_nfc(self, card):
        self.enable_nfc()
        path = os.path.join(self.workdir, "card.vcf")
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(card)
        # ndef-share emulates a Type 4 tag and exits once a reader got the whole message
        self.share_proc = subprocess.Popen(["ndef-share", "-m", "text/vcard", path],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.share_watch = GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.share_proc.pid,
                                                self.on_share_exit)

    def on_share_exit(self, pid, status):
        proc = self.share_proc
        self.share_proc = None
        self.share_watch = 0
        if proc is None:
            return
        err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
        proc.stderr and proc.stderr.close()
        try:
            GLib.spawn_check_wait_status(status)
            ok = True
        except GLib.Error:
            ok = False
        if ok and self.state == "sharing":
            log("The card was read over NFC")
            self.session.emit_signal(None, OBJECT_PATH, BUS_NAME, "CardRead",
                                     GLib.Variant("(s)", ("nfc",)))
            self.feedback("message-sent-instant")
            self.stop()
        elif self.state == "sharing":
            log(f"ndef-share ended: {err}")
            self.channels = [c for c in self.channels if c != "nfc"]
            self.error = _("NFC is not available, the QR code still works")
            self.changed("Channels", "Error")

    def stop_share_nfc(self):
        proc = self.share_proc
        self.share_proc = None
        if self.share_watch:
            GLib.source_remove(self.share_watch)
            self.share_watch = 0
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc and proc.stderr:
            proc.stderr.close()

    def receive_nfc(self):
        self.enable_nfc()
        self.seen_tags = set()
        self.tags_sub = self.system.signal_subscribe(
            NFC_DAEMON, NFC_ADAPTER_IFACE, "TagsChanged", NFC_ADAPTER, None,
            Gio.DBusSignalFlags.NONE, lambda *a: self.on_tags(a[5][0]))
        tags = self.call(self.system, NFC_DAEMON, NFC_ADAPTER, NFC_ADAPTER_IFACE, "GetTags",
                         None, "(ao)")[0]
        self.on_tags(tags)

    def on_tags(self, tags):
        for tag in tags:
            if tag in self.seen_tags:
                continue
            self.seen_tags.add(tag)
            try:
                info = self.call(self.system, NFC_DAEMON, tag, NFC_TAG_IFACE, "GetAll", None,
                                 "(ibuuuasao)")
            except GLib.Error as e:
                log(f"Reading a tag failed: {e.message}")
                continue
            for record in info[6]:
                try:
                    (_v, _flags, tnf, _ifaces, rtype, _rid, payload) = self.call(
                        self.system, NFC_DAEMON, record, NFC_NDEF_IFACE, "GetAll", None,
                        "(iuuasayayay)")
                except GLib.Error as e:
                    log(f"Reading a record failed: {e.message}")
                    continue
                rtype = bytes(rtype).decode("ascii", "replace").lower()
                # TNF 2: a MIME record
                if tnf == 2 and rtype in VCARD_TYPES and len(payload) <= MAX_CARD_BYTES:
                    self.add_received(bytes(payload).decode("utf-8", "replace"), "nfc")
                else:
                    log(f"Tag record TNF {tnf} type {rtype} is not a card")

    def stop_receive_nfc(self):
        if self.tags_sub:
            self.system.signal_unsubscribe(self.tags_sub)
            self.tags_sub = 0
        self.seen_tags = set()

    # ----- Bluetooth -----

    def bluetooth_name(self):
        try:
            if not self.get_prop(self.system, BLUEZ, BLUEZ_ADAPTER, BLUEZ_ADAPTER_IFACE,
                                 "Powered"):
                return ""
            return self.get_prop(self.system, BLUEZ, BLUEZ_ADAPTER, BLUEZ_ADAPTER_IFACE,
                                 "Alias")
        except GLib.Error:
            return ""

    def set_discoverable_timeout(self):
        self.set_prop(self.system, BLUEZ, BLUEZ_ADAPTER, BLUEZ_ADAPTER_IFACE,
                      "DiscoverableTimeout", GLib.Variant("u", self.remaining + 5))

    def receive_bluetooth(self):
        """Visible for the session and an OBEX agent that takes cards, and only cards."""
        if not self.bluetooth_name():
            raise CardError(_("Bluetooth is off"))
        self.agent_reg = self.session.register_object(
            AGENT_PATH, self.agent_node.interfaces[0], self.on_agent_call, None, None)
        try:
            self.call(self.session, OBEX, "/org/bluez/obex", "org.bluez.obex.AgentManager1",
                      "RegisterAgent", GLib.Variant("(o)", (AGENT_PATH,)))
        except GLib.Error:
            self.session.unregister_object(self.agent_reg)
            self.agent_reg = 0
            raise
        # Visibility: back to how it was afterwards (bluez also ends it after the timeout)
        self.bt_was_discoverable = self.get_prop(self.system, BLUEZ, BLUEZ_ADAPTER,
                                                 BLUEZ_ADAPTER_IFACE, "Discoverable")
        self.bt_timeout_before = self.get_prop(self.system, BLUEZ, BLUEZ_ADAPTER,
                                               BLUEZ_ADAPTER_IFACE, "DiscoverableTimeout")
        if not self.bt_was_discoverable:
            self.set_discoverable_timeout()
            self.set_prop(self.system, BLUEZ, BLUEZ_ADAPTER, BLUEZ_ADAPTER_IFACE,
                          "Discoverable", GLib.Variant("b", True))

    def on_agent_call(self, conn, sender, path, iface, method, params, invocation):
        if method == "AuthorizePush":
            self.authorize_push(params[0], invocation)
        else:
            invocation.return_value(None)

    def authorize_push(self, transfer, invocation):
        try:
            props = self.call(self.session, OBEX, transfer, "org.freedesktop.DBus.Properties",
                              "GetAll", GLib.Variant("(s)", (OBEX_TRANSFER_IFACE,)),
                              "(a{sv})")[0]
        except GLib.Error as e:
            log(f"Transfer unreadable: {e.message}")
            invocation.return_dbus_error("org.bluez.obex.Error.Rejected", "unreadable")
            return
        name = os.path.basename(props.get("Name") or "")
        ftype = (props.get("Type") or "").lower()
        size = props.get("Size") or 0
        if (self.state != "receiving" or size > MAX_CARD_BYTES or
                not (name.lower().endswith(".vcf") or ftype in VCARD_TYPES)):
            log(f"Bluetooth push of type {ftype or '?'} ({size} bytes) rejected: not a card")
            invocation.return_dbus_error("org.bluez.obex.Error.Rejected", "Only contact cards")
            return
        # obexd writes into its own folder, the file only lives until the card is read
        folder = os.path.join(GLib.get_user_cache_dir(), "obexd", "plinius-cardshare")
        os.makedirs(folder, mode=0o700, exist_ok=True)
        self.pushes += 1
        target = os.path.join(folder, f"card-{self.pushes}-{os.getpid()}.vcf")
        sub = self.session.signal_subscribe(
            OBEX, "org.freedesktop.DBus.Properties", "PropertiesChanged", transfer, None,
            Gio.DBusSignalFlags.NONE, self.on_transfer_changed)
        self.transfers[transfer] = (target, sub)
        log(f"Bluetooth push of a card ({size} bytes) accepted")
        invocation.return_value(GLib.Variant("(s)", (target,)))

    def on_transfer_changed(self, conn, sender, path, iface, signal_name, params):
        status = params[1].get("Status")
        if status not in ("complete", "error") or path not in self.transfers:
            return
        target, sub = self.transfers.pop(path)
        self.session.signal_unsubscribe(sub)
        try:
            if status == "complete":
                with open(target, "rb") as f:
                    self.add_received(f.read(MAX_CARD_BYTES).decode("utf-8", "replace"),
                                      "bluetooth")
            else:
                log("Bluetooth transfer failed")
        except OSError as e:
            log(f"Received card unreadable: {e}")
        finally:
            try:
                os.unlink(target)
            except OSError:
                pass

    def stop_receive_bluetooth(self):
        if self.agent_reg:
            try:
                self.call(self.session, OBEX, "/org/bluez/obex", "org.bluez.obex.AgentManager1",
                          "UnregisterAgent", GLib.Variant("(o)", (AGENT_PATH,)))
            except GLib.Error as e:
                log(f"Unregistering the OBEX agent failed: {e.message}")
            self.session.unregister_object(self.agent_reg)
            self.agent_reg = 0
        if self.bt_was_discoverable is False:
            try:
                self.set_prop(self.system, BLUEZ, BLUEZ_ADAPTER, BLUEZ_ADAPTER_IFACE,
                              "Discoverable", GLib.Variant("b", False))
                if self.bt_timeout_before is not None:
                    self.set_prop(self.system, BLUEZ, BLUEZ_ADAPTER, BLUEZ_ADAPTER_IFACE,
                                  "DiscoverableTimeout", GLib.Variant("u", self.bt_timeout_before))
            except GLib.Error as e:
                log(f"Hiding Bluetooth again failed: {e.message}")
        self.bt_was_discoverable = None
        self.bt_timeout_before = None

    # ----- Screen, feedback, notifications -----

    def inhibit_idle(self):
        """The screen stays on: with the display off, NFC stops polling."""
        try:
            self.inhibit_cookie = self.call(
                self.session, "org.gnome.SessionManager", "/org/gnome/SessionManager",
                "org.gnome.SessionManager", "Inhibit",
                GLib.Variant("(susu)", ("org.plinius.CardShare", 0,
                                        _("Sharing a contact card"), 8)), "(u)")[0]
        except GLib.Error as e:
            log(f"Keeping the screen on failed: {e.message}")

    def uninhibit_idle(self):
        if not self.inhibit_cookie:
            return
        try:
            self.call(self.session, "org.gnome.SessionManager", "/org/gnome/SessionManager",
                      "org.gnome.SessionManager", "Uninhibit",
                      GLib.Variant("(u)", (self.inhibit_cookie,)))
        except GLib.Error as e:
            log(f"Releasing the screen failed: {e.message}")
        self.inhibit_cookie = 0

    def feedback(self, event):
        try:
            self.call(self.session, "org.sigxcpu.Feedback", "/org/sigxcpu/Feedback",
                      "org.sigxcpu.Feedback", "TriggerFeedback",
                      GLib.Variant("(ssa{sv}i)", ("org.plinius.CardShare", event, {}, -1)),
                      "(u)")
        except GLib.Error as e:
            log(f"Feedback failed: {e.message}")

    def notify_received(self, card_id, name, detail):
        body = detail or _("A contact card came in")
        try:
            nid = self.call(
                self.session, "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                "org.freedesktop.Notifications", "Notify",
                GLib.Variant("(susssasa{sv}i)", (
                    _("Contact Card"), 0, "contact-new-symbolic", name, body,
                    ["import", _("Add to Contacts"), "dismiss", _("Dismiss")],
                    {"category": GLib.Variant("s", "transfer.complete"),
                     "desktop-entry": GLib.Variant("s", "org.gnome.Contacts")}, 0)),
                "(u)")[0]
            self.notifications[nid] = card_id
        except GLib.Error as e:
            log(f"Notification failed: {e.message}")

    def close_notification(self, nid):
        self.notifications.pop(nid, None)
        try:
            self.call(self.session, "org.freedesktop.Notifications",
                      "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
                      "CloseNotification", GLib.Variant("(u)", (nid,)))
        except GLib.Error:
            pass

    def on_notification_action(self, conn, sender, path, iface, signal_name, params):
        nid, action = params[0], params[1]
        card_id = self.notifications.pop(nid, None)
        if card_id is None:
            return
        try:
            if action == "import":
                self.import_card(card_id)
            elif action == "dismiss":
                self.dismiss(card_id)
        except (CardError, GLib.Error) as e:
            log(f"Notification action failed: {getattr(e, 'message', e)}")
        self.schedule_idle_exit()

    def on_notification_closed(self, conn, sender, path, iface, signal_name, params):
        self.notifications.pop(params[0], None)

    def shutdown(self):
        self.stop()
        for nid in list(self.notifications):
            self.close_notification(nid)


def main():
    loop = GLib.MainLoop()
    service = None

    def on_bus_acquired(conn, name):
        nonlocal service
        service = CardShare(loop)

    def on_name_lost(conn, name):
        log("The bus name is taken, exiting")
        loop.quit()

    Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                     on_bus_acquired, None, on_name_lost)
    for signum in (signal.SIGTERM, signal.SIGINT):
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signum, lambda: loop.quit() or False)
    loop.run()
    if service:
        service.shutdown()


if __name__ == "__main__":
    main()
