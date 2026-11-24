#!/usr/bin/env python3
"""Run mmsd4ofono end to end against stand-ins, without a carrier.

MMS is switched off in Germany (Vodafone 2023, Telekom, O2 and 1&1 on
2026-06-30), so a real test needs a SIM from a country that still has it.
This script tests everything on the phone's side instead:

  * a stand-in oFono on a private bus (modem, SIM, an MMS context, push agent),
  * a stand-in route daemon (MMSD_ROUTECTL_SOCKET, patch 0003),
  * a stand-in MMSC on 127.0.0.1 that hands out one message and takes one,

and drives the installed /usr/bin/mmsd with them:

  * an incoming notification is fetched, confirmed to the MMSC with
    m-notifyresp.ind and published on D-Bus; a repeat of it is not fetched
    again, one pointing at a link-local address not at all (patch 0006),
  * only the MMSC of the MMS context is fetched from, none at all without
    one, and a redirect of the MMSC is not followed (patch 0010),
  * a message is sent and confirmed,
  * the MMS context goes up for each and down again afterwards (upstream PR #10),
  * an incoming message is published while it downloads and filled in once it
    is there (upstream PR #11); a refused notification is not published,
  * an MMSC set as Chatty sets it reaches the oFono MMS context, the own
    number is kept in mmsd's settings (patch 0005),
  * mmsd asks oFono for the data SIM's modem and restarts when the data SIM
    moves (patch 0007).

Nothing touches the real oFono, the real routes or the user's messages: mmsd
gets a private system bus, a private session bus and a HOME of its own in a
temporary directory. Run it as the normal user, with the package installed:

    python3 -I tools/mms-standin.py [--keep]
"""

import argparse
import asyncio
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from array import array

MMSD_LIB = "/usr/lib/mmsd"
MMSD_BIN = "/usr/bin/mmsd"

try:
    from aiohttp import web
    from dbus_fast import Message, MessageType, Variant
    from dbus_fast.aio import MessageBus
    from dbus_fast.service import ServiceInterface, method, signal as dbus_signal
except ImportError as e:
    sys.exit(f"missing module ({e}): install mmsd4ofono first")

if not os.path.isfile(os.path.join(MMSD_LIB, "main.py")):
    sys.exit(f"{MMSD_LIB} not found: install mmsd4ofono first")
sys.path.insert(0, MMSD_LIB)
from mmsdecoder.message import MMSMessage, MMSMessagePage  # noqa: E402

MODEM = "/ril_0"
CTX_INTERNET = MODEM + "/context1"
CTX_MMS = MODEM + "/context2"
OWN_NUMBER = "+4915100000001"
SENDER = "+4915100000002"
RECIPIENT = "+4915100000003"
TEXT_IN = "Stand-in MMS: hello from the MMSC"
TEXT_OUT = "Stand-in MMS: reply from the phone"
# 1x1 GIF, the classic MMS picture format
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
# mmsd's LINGER (patch 0001) plus room
TEARDOWN_WAIT = 45
# Patch 0005: as Chatty's MMS settings write them
NEW_MMSC = "http://mmsc.standin.invalid/servlets/mms"
NEW_NUMBER = "+4915100000099"

log_lines = []


def log(text):
    line = f"{time.strftime('%H:%M:%S')} {text}"
    log_lines.append(line)
    print(line, flush=True)


# --- PDUs ---------------------------------------------------------------------

def _text(value):
    return value.encode() + b"\0"


def notification_ind(tid, sender, url, size):
    """m-notification-ind as a carrier sends it in a WAP push (OMA-MMS-ENC 1.2)."""
    pdu = bytes([0x8C, 0x82]) + bytes([0x98]) + _text(tid) + bytes([0x8D, 0x92])
    frm = bytes([0x80]) + _text(sender + "/TYPE=PLMN")
    pdu += bytes([0x89, len(frm)]) + frm
    pdu += bytes([0x8A, 0x80])                                  # Message-Class Personal
    pdu += bytes([0x8E, 0x03]) + size.to_bytes(3, "big")        # Message-Size
    expiry = bytes([0x81, 0x03]) + (86400).to_bytes(3, "big")   # relative, one day
    pdu += bytes([0x88, len(expiry)]) + expiry
    pdu += bytes([0x83]) + _text(url)                           # Content-Location
    return pdu


def retrieve_conf(tid, sender, gif_path):
    mms = MMSMessage()
    mms.headers["Message-Type"] = "m-retrieve-conf"
    mms.headers["Transaction-Id"] = tid
    mms.headers["MMS-Version"] = "1.2"
    mms.headers["From"] = sender + "/TYPE=PLMN"
    mms.headers["To"] = [OWN_NUMBER + "/TYPE=PLMN"]
    mms.headers["Message-Class"] = "Personal"
    # add_page() looks at the Content-Type, so it has to be there first
    mms.headers["Content-Type"] = ("application/vnd.wap.multipart.related",
                                   {"Type": "application/smil", "Start": "<0000>"})
    page = MMSMessagePage()
    page.add_text(TEXT_IN)
    page.add_image(gif_path)
    mms.add_page(page)
    return mms.encode().tobytes()


def send_conf(tid, message_id):
    return (bytes([0x8C, 0x81, 0x98]) + _text(tid) + bytes([0x8D, 0x92])
            + bytes([0x92, 0x80])                               # Response-Status Ok
            + bytes([0x8B]) + _text(message_id))


def decode(pdu):
    return MMSMessage.from_data(array("B", pdu))


def check_pdus(gif_path):
    """The stand-ins are only as good as their PDUs: decode them with mmsd's own decoder."""
    url = "http://127.0.0.1:1/mms/T1"
    n = decode(notification_ind("T1", SENDER, url, 1234)).headers
    assert n.get("Message-Type") == "m-notification-ind", n
    assert n.get("Content-Location") == url, n
    assert str(n.get("From", "")).startswith(SENDER), n
    r = decode(retrieve_conf("T1", SENDER, gif_path))
    assert r.headers.get("Message-Type") == "m-retrieve-conf", r.headers
    types = [p.content_type for p in r.data_parts]
    assert "text/plain" in types and "image/gif" in types, types
    c = decode(send_conf("T2", "M2")).headers
    assert c.get("Message-Type") == "m-send-conf" and c.get("Response-Status") == "Ok", c


# --- stand-in oFono -------------------------------------------------------------

class Props(ServiceInterface):
    """An oFono interface with GetProperties/SetProperty/PropertyChanged."""

    def __init__(self, name, props, on_set=None):
        super().__init__(name)
        self.props = props
        self.on_set = on_set

    @method()
    def GetProperties(self) -> "a{sv}":
        return self.props

    @method()
    def SetProperty(self, prop: "s", value: "v"):
        self.props[prop] = value
        self.PropertyChanged(prop, value)
        if self.on_set:
            self.on_set(prop, value)

    @dbus_signal()
    def PropertyChanged(self, name, value) -> "sv":
        return [name, value]


class Manager(ServiceInterface):
    def __init__(self, modem_props):
        super().__init__("org.ofono.Manager")
        self.modem_props = modem_props

    @method()
    def GetModems(self) -> "a(oa{sv})":
        return [[MODEM, self.modem_props]]

    @dbus_signal()
    def ModemAdded(self, path, props) -> "oa{sv}":
        return [path, props]

    @dbus_signal()
    def ModemRemoved(self, path) -> "o":
        return path


class ConnectionManager(Props):
    def __init__(self, contexts):
        super().__init__("org.ofono.ConnectionManager",
                         {"Attached": Variant("b", True), "Powered": Variant("b", True)})
        self.contexts = contexts

    @method()
    def GetContexts(self) -> "a(oa{sv})":
        return [[path, ctx.props] for path, ctx in self.contexts.items()]


class SlotManager(ServiceInterface):
    """The part of Sailfish oFono's org.nemomobile.ofono.ModemManager mmsd uses."""

    def __init__(self):
        super().__init__("org.nemomobile.ofono.ModemManager")
        self.asked = 0

    @method()
    def GetDefaultDataModem(self) -> "s":
        self.asked += 1
        return MODEM

    @dbus_signal()
    def DefaultDataModemChanged(self, path) -> "s":
        return path


class PushNotification(ServiceInterface):
    def __init__(self):
        super().__init__("org.ofono.PushNotification")

    @method()
    def RegisterAgent(self, path: "o"):
        log(f"oFono: push agent registered at {path}")

    @method()
    def UnregisterAgent(self, path: "o"):
        log(f"oFono: push agent unregistered at {path}")


class StandInOfono:
    def __init__(self, mmsc_url):
        self.events = []        # (time, "up"/"down") of the MMS context
        self.agent = None       # unique bus name of mmsd's push agent
        self.agent_ready = asyncio.Event()
        self.mms_ctx = Props("org.ofono.ConnectionContext", {
            "Name": Variant("s", "MMS"), "Type": Variant("s", "mms"),
            "AccessPointName": Variant("s", "mms.standin"), "Protocol": Variant("s", "ip"),
            "MessageCenter": Variant("s", mmsc_url), "MessageProxy": Variant("s", ""),
            "Active": Variant("b", False), "Settings": Variant("a{sv}", {}),
        }, self.mms_set)
        self.internet_ctx = Props("org.ofono.ConnectionContext", {
            "Name": Variant("s", "Internet"), "Type": Variant("s", "internet"),
            "AccessPointName": Variant("s", "internet.standin"), "Protocol": Variant("s", "ip"),
            "Active": Variant("b", True),
            "Settings": Variant("a{sv}", {"Interface": Variant("s", "lo")}),
        })
        interfaces = ["org.ofono.SimManager", "org.ofono.ConnectionManager",
                      "org.ofono.PushNotification"]
        self.modem_props = {"Interfaces": Variant("as", interfaces),
                            "Powered": Variant("b", True), "Online": Variant("b", True)}

    def mms_set(self, prop, value):
        if prop != "Active":
            return
        up = bool(value.value)
        self.events.append((time.monotonic(), "up" if up else "down"))
        log(f"oFono: MMS context {'activated' if up else 'deactivated'}")
        settings = {"Interface": Variant("s", "lo")} if up else {}
        # oFono reports Active first and Settings a little later
        asyncio.get_event_loop().call_later(
            0.3, self.mms_ctx.PropertyChanged, "Settings", Variant("a{sv}", settings))
        self.mms_ctx.props["Settings"] = Variant("a{sv}", settings)

    def watch(self, msg):
        if (msg.message_type == MessageType.METHOD_CALL and msg.member == "RegisterAgent"
                and msg.interface == "org.ofono.PushNotification"):
            self.agent = msg.sender
            self.agent_ready.set()
        return None

    async def start(self, address):
        self.bus = await MessageBus(bus_address=address).connect()
        self.bus.add_message_handler(self.watch)
        self.bus.export("/", Manager(self.modem_props))
        self.slots = SlotManager()
        self.bus.export("/", self.slots)
        self.bus.export(MODEM, Props("org.ofono.Modem", self.modem_props))
        self.bus.export(MODEM, Props("org.ofono.SimManager", {
            "Present": Variant("b", True), "PinRequired": Variant("s", "none"),
            "SubscriberNumbers": Variant("as", [OWN_NUMBER])}))
        self.bus.export(MODEM, ConnectionManager({CTX_INTERNET: self.internet_ctx,
                                                  CTX_MMS: self.mms_ctx}))
        self.bus.export(MODEM, PushNotification())
        self.bus.export(CTX_INTERNET, self.internet_ctx)
        self.bus.export(CTX_MMS, self.mms_ctx)
        await self.bus.request_name("org.ofono")


# --- stand-in route daemon and MMSC ---------------------------------------------

class StandInRoutectl:
    def __init__(self):
        self.requests = []

    async def handle(self, reader, writer):
        line = await reader.readline()
        try:
            request = json.loads(line.decode())
        except ValueError:
            request = {"action": "?"}
        self.requests.append(request)
        log(f"routectld: {request.get('action')} {request.get('ips', '')}")
        if request.get("action") == "setup":
            writer.write(b'{"ok": true}\n')
            await writer.drain()
        writer.close()

    async def start(self, path):
        self.server = await asyncio.start_unix_server(self.handle, path=path)


class StandInMMSC:
    def __init__(self, gif_path):
        self.gif_path = gif_path
        self.fetched = []
        self.sent = []
        self.acks = []          # (transaction ID, X-Mms-Status) of each m-notifyresp.ind
        self.redirected = []    # transaction IDs answered with a redirect

    async def get(self, request):
        tid = request.match_info["tid"]
        self.fetched.append(tid)
        log(f"MMSC: GET {request.path}")
        return web.Response(body=retrieve_conf(tid, SENDER, self.gif_path),
                            content_type="application/vnd.wap.mms-message")

    async def redirect(self, request):
        # As a notification's server would answer to lead mmsd on to 127.0.0.1 or the home
        # network; here to a message of this MMSC, so a followed redirect shows in fetched
        tid = request.match_info["tid"]
        self.redirected.append(tid)
        log(f"MMSC: GET {request.path} -> 302")
        raise web.HTTPFound(f"http://127.0.0.1:{self.port}/mms/{tid}R")

    async def post(self, request):
        body = await request.read()
        if body[:3] == bytes([0x8C, 0x83, 0x98]):
            # m-notifyresp.ind: headers only, taken apart by hand (OMA-MMS-ENC 1.2, 6.2)
            end = body.index(0, 3)
            tid = body[3:end].decode(errors="replace")
            rest = body[end + 1:]
            status = rest[rest.index(0x95) + 1] if 0x95 in rest else None
            self.acks.append((tid, status))
            log(f"MMSC: m-notifyresp.ind for {tid}, status {status:#x}" if status is not None
                else f"MMSC: m-notifyresp.ind for {tid} without a status")
            return web.Response(status=204)
        mms = decode(body)
        self.sent.append(mms)
        tid = mms.headers.get("Transaction-Id", "")
        log(f"MMSC: POST {len(body)} bytes, {mms.headers.get('Message-Type')} to {mms.headers.get('To')}")
        return web.Response(body=send_conf(tid, "standin-" + tid[:8]),
                            content_type="application/vnd.wap.mms-message")

    async def start(self):
        app = web.Application()
        app.router.add_get("/mms/redirect/{tid}", self.redirect)
        app.router.add_get("/mms/{tid}", self.get)
        app.router.add_post("/mms", self.post)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        await web.SockSite(self.runner, sock).start()


# --- harness --------------------------------------------------------------------

BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={path}</listen>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def start_bus(tmp, name):
    path = os.path.join(tmp, name + ".bus")
    config = os.path.join(tmp, name + ".conf")
    with open(config, "w", encoding="utf-8") as f:
        f.write(BUS_CONFIG.format(path=path))
    proc = subprocess.Popen(["dbus-daemon", "--config-file=" + config, "--nofork",
                             "--print-address=1"], stdout=subprocess.PIPE, text=True)
    address = proc.stdout.readline().strip()
    if not address:
        raise RuntimeError(f"dbus-daemon for {name} did not start")
    return proc, address


async def wait_for(predicate, timeout, step=0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


async def add_match(bus, rule):
    await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                           interface="org.freedesktop.DBus", member="AddMatch",
                           signature="s", body=[rule]))


async def name_has_owner(bus, name):
    reply = await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                   interface="org.freedesktop.DBus", member="NameHasOwner",
                                   signature="s", body=[name]))
    return reply.body[0]


def context_down_after(ofono, since):
    downs = [t for t, e in ofono.events if e == "down" and t >= since]
    ups = [t for t, e in ofono.events if e == "up" and t >= since]
    return bool(downs) and (not ups or downs[-1] > ups[-1])


async def run(tmp):
    results = []

    def result(name, ok, detail=""):
        results.append((name, ok))
        log(f"{'PASS' if ok else 'FAIL'}: {name}{' (' + detail + ')' if detail else ''}")

    gif_path = os.path.join(tmp, "picture.gif")
    with open(gif_path, "wb") as f:
        f.write(GIF)
    check_pdus(gif_path)
    result("stand-in PDUs decode with mmsd's decoder", True)

    system_proc, system_address = start_bus(tmp, "system")
    session_proc, session_address = start_bus(tmp, "session")
    mmsd = None
    try:
        mmsc = StandInMMSC(gif_path)
        await mmsc.start()
        mmsc_url = f"http://127.0.0.1:{mmsc.port}/mms"
        ofono = StandInOfono(mmsc_url)
        await ofono.start(system_address)
        routectl = StandInRoutectl()
        routectl_path = os.path.join(tmp, "routectl.sock")
        await routectl.start(routectl_path)

        home = os.path.join(tmp, "home")
        os.makedirs(home)
        env = dict(os.environ, HOME=home, DBUS_SYSTEM_BUS_ADDRESS=system_address,
                   DBUS_SESSION_BUS_ADDRESS=session_address,
                   MMSD_ROUTECTL_SOCKET=routectl_path, MODEM_DEBUG="true",
                   MMSD_ALLOW_LOOPBACK_MMSC="1")
        mmsd_log = open(os.path.join(tmp, "mmsd.log"), "w", encoding="utf-8")
        mmsd = subprocess.Popen([MMSD_BIN], env=env, stdout=mmsd_log, stderr=subprocess.STDOUT)
        log(f"mmsd started (pid {mmsd.pid}), MMSC at {mmsc_url}")

        try:
            await asyncio.wait_for(ofono.agent_ready.wait(), 60)
            result("mmsd registers its push agent with oFono", True)
        except asyncio.TimeoutError:
            result("mmsd registers its push agent with oFono", False, "no RegisterAgent in 60 s")
            return results

        session = await MessageBus(bus_address=session_address).connect()
        signals = []
        session.add_message_handler(
            lambda m: signals.append(m) if m.message_type == MessageType.SIGNAL else None)
        await add_match(session, "type='signal',interface='org.ofono.mms.Service'")
        await add_match(session, "type='signal',interface='org.ofono.mms.Message'")
        owned = False
        for _ in range(150):
            if await name_has_owner(session, "org.ofono.mms"):
                owned = True
                break
            await asyncio.sleep(0.2)
        result("mmsd owns org.ofono.mms on the session bus", owned)
        if not owned:
            return results
        result("mmsd asks oFono for the data SIM's modem", ofono.slots.asked > 0)

        async def push(tid, url):
            """A WAP push with a notification, as oFono hands it over. The call
            returns once mmsd has fetched (or refused) the message."""
            now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            return await ofono.bus.call(Message(
                destination=ofono.agent, path="/mmsd", interface="org.ofono.PushNotificationAgent",
                member="ReceiveNotification", signature="aya{sv}",
                body=[notification_ind(tid, SENDER, url, len(GIF) + 200),
                      {"Sender": Variant("s", SENDER), "SentTime": Variant("s", now),
                       "LocalSentTime": Variant("s", now)}]))

        def added():
            return [m for m in signals if m.member == "MessageAdded"]

        # 1. Receive
        start = time.monotonic()
        tid = "STANDIN" + str(int(time.time()))
        url = f"{mmsc_url}/{tid}"
        await push(tid, url)
        await wait_for(lambda: added(), 60)
        result("the message is fetched from the MMSC", tid in mmsc.fetched)
        result("the MMSC gets m-notifyresp.ind with status Retrieved", (tid, 0x81) in mmsc.acks,
               f"got {mmsc.acks}")
        result("the MMS context went up for the fetch",
               any(e == "up" and t >= start for t, e in ofono.events))
        result("routes were asked for the MMSC only",
               bool(routectl.requests) and all(r.get("ips") == ["127.0.0.1"]
                                               for r in routectl.requests if r.get("action") == "setup"))
        def current(path, props):
            """The properties of a message as a client sees them: MessageAdded, then each
            PropertyChanged on its path. A message is added while it downloads and gets its
            sender, text and parts in place once it is there (upstream PR #11)."""
            props = dict(props)
            for m in signals:
                if m.path == path and m.member == "PropertyChanged":
                    name = "Status" if m.body[0] == "status" else m.body[0]
                    props[name] = m.body[1]
            return props

        def received():
            return any(m.member == "PropertyChanged" and m.body[0] == "status"
                       and m.body[1].value == "received" for m in signals) or any(
                       m.body[1]["Status"].value == "received" for m in added())

        if added():
            await wait_for(received, 60)
            path, props = added()[0].body
            result("MessageAdded: published while it downloads, then filled in",
                   props["Status"].value in ("downloading", "received"),
                   f"status {props['Status'].value}")
            props = current(path, props)
            sender = props["Sender"].value
            attachments = props["Attachments"].value
            texts = []
            for _id, ctype, file_path, offset, length in attachments:
                if ctype.startswith("text/plain"):
                    with open(file_path, "rb") as f:
                        f.seek(offset)
                        texts.append(f.read(length).decode())
            ctypes = [a[1] for a in attachments]
            result("the message: from the sender, as received",
                   sender == SENDER and props["Status"].value == "received",
                   f"sender {sender}, status {props['Status'].value}")
            result("the message: text and picture", TEXT_IN in texts and "image/gif" in ctypes,
                   f"parts {ctypes}")
        else:
            result("MessageAdded for the received message", False, "no signal in 60 s")

        # The same notification again, as an MMSC sends it that missed the confirmation
        await push(tid, url)
        await asyncio.sleep(2)
        result("a repeated notification is not fetched again",
               mmsc.fetched.count(tid) == 1 and len(added()) == 1,
               f"{mmsc.fetched.count(tid)} fetches, {len(added())} messages")

        ok = await wait_for(lambda: context_down_after(ofono, start), TEARDOWN_WAIT)
        result("the MMS context went down after the fetch", ok)

        # A notification is an SMS anyone can send: one that points at this device's link
        start = time.monotonic()
        before, shown = len(routectl.requests), len(added())
        await push(tid + "L", f"http://169.254.1.1/mms/{tid}L")
        await asyncio.sleep(2)
        result("a link-local Content-Location gets neither route nor bearer, nor a message",
               len(routectl.requests) == before and len(added()) == shown
               and not any(e == "up" and t >= start for t, e in ofono.events),
               f"requests {routectl.requests[before:]}")

        # Patch 0010: only the carrier's MMSC, not even a DNS query for another host
        start = time.monotonic()
        before, shown = len(routectl.requests), len(added())
        await push(tid + "F", f"http://mms.attacker.invalid/mms/{tid}F")
        await asyncio.sleep(2)
        result("a Content-Location on another host than the MMSC gets neither route nor bearer, nor a message",
               len(routectl.requests) == before and len(added()) == shown
               and not any(e == "up" and t >= start for t, e in ofono.events),
               f"requests {routectl.requests[before:]}")

        # Patch 0010: a redirect is not followed and not tried again
        await push(tid + "D", f"{mmsc_url}/redirect/{tid}D")
        await wait_for(lambda: mmsc.redirected, 30)
        await asyncio.sleep(3)
        result("a redirect of the MMSC is not followed",
               mmsc.redirected == [tid + "D"] and tid + "DR" not in mmsc.fetched,
               f"redirected {mmsc.redirected}, fetched {mmsc.fetched}")
        ok = await wait_for(lambda: context_down_after(ofono, start), TEARDOWN_WAIT)
        result("the MMS context went down after the redirect", ok)

        # Patch 0010: no MMSC in the MMS context (MMS switched off): nothing is fetched
        saved_mmsc = ofono.mms_ctx.props["MessageCenter"]
        ofono.mms_ctx.props["MessageCenter"] = Variant("s", "")
        start = time.monotonic()
        before, shown = len(routectl.requests), len(added())
        await push(tid + "N", f"{mmsc_url}/{tid}N")
        await asyncio.sleep(2)
        ofono.mms_ctx.props["MessageCenter"] = saved_mmsc
        result("without an MMSC nothing is fetched or shown",
               tid + "N" not in mmsc.fetched and len(routectl.requests) == before
               and len(added()) == shown
               and not any(e == "up" and t >= start for t, e in ofono.events),
               f"fetched {mmsc.fetched}, requests {routectl.requests[before:]}")

        # 2. Send: as Chatty does it
        start = time.monotonic()
        text_path = os.path.join(tmp, "reply.txt")
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(TEXT_OUT)
        reply = await session.call(Message(
            destination="org.ofono.mms", path="/org/ofono/mms/modemmanager",
            interface="org.ofono.mms.Service", member="SendMessage", signature="asva(sss)",
            body=[[RECIPIENT], Variant("s", ""), [["text-1", "text/plain", text_path]]]))
        if reply.message_type == MessageType.ERROR:
            result("SendMessage is accepted", False, str(reply.body))
            return results
        message_path = reply.body[0]

        def status_sent():
            for m in signals:
                if (m.path == message_path and m.member == "PropertyChanged"
                        and m.body[0].lower() == "status" and m.body[1].value == "sent"):
                    return True
            return False

        sent = await wait_for(status_sent, 60)
        result("the MMSC gets an m-send-req for the recipient",
               bool(mmsc.sent) and mmsc.sent[-1].headers.get("Message-Type") == "m-send-req"
               and any(RECIPIENT in str(t) for t in mmsc.sent[-1].headers.get("To", [])))
        if mmsc.sent:
            parts = [p.data.decode(errors="replace") for p in mmsc.sent[-1].data_parts
                     if p.content_type.startswith("text/plain")]
            result("the sent text arrives unchanged", TEXT_OUT in parts)
        result("the message turns to sent after m-send-conf", sent)
        result("the MMS context went up for the send",
               any(e == "up" and t >= start for t, e in ofono.events))
        ok = await wait_for(lambda: context_down_after(ofono, start), TEARDOWN_WAIT)
        result("the MMS context went down after the send", ok)

        # 3. Settings, as Chatty's MMS settings change them
        async def change(setting, value):
            return await session.call(Message(
                destination="org.ofono.mms", path="/org/ofono/mms",
                interface="org.ofono.mms.ModemManager", member="ChangeSettings",
                signature="sv", body=[setting, Variant("s", value)]))

        reply = await change("CarrierMMSC", NEW_MMSC)
        result("ChangeSettings CarrierMMSC is accepted", reply.message_type != MessageType.ERROR,
               str(reply.body))
        result("the MMSC reaches the oFono MMS context",
               ofono.mms_ctx.props["MessageCenter"].value == NEW_MMSC,
               ofono.mms_ctx.props["MessageCenter"].value)
        reply = await change("ModemNumber", NEW_NUMBER)
        settings_file = os.path.join(home, ".mms", "modemmanager", "mms")
        try:
            with open(settings_file, encoding="utf-8") as f:
                saved = f.read()
        except OSError:
            saved = ""
        result("the own number is saved for the next start",
               reply.message_type != MessageType.ERROR and f"ModemNumber={NEW_NUMBER}" in saved)

        # 4. The data SIM moves to the other modem: mmsd exits, systemd would start it again
        ofono.slots.DefaultDataModemChanged("/ril_1")
        exited = await wait_for(lambda: mmsd.poll() is not None, 15)
        result("mmsd exits cleanly when the data SIM moves",
               exited and mmsd.returncode == 0, f"exit code {mmsd.poll()}")
        return results
    finally:
        if mmsd and mmsd.poll() is None:
            mmsd.send_signal(signal.SIGTERM)
            try:
                mmsd.wait(5)
            except subprocess.TimeoutExpired:
                mmsd.kill()
        for proc in (session_proc, system_proc):
            proc.terminate()
            proc.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true",
                        help="keep the temporary directory (mmsd.log, mmsd's HOME)")
    args = parser.parse_args()
    if os.geteuid() == 0:
        sys.exit("run as the normal user: mmsd is a user service")
    if not shutil.which("dbus-daemon"):
        sys.exit("dbus-daemon not found")

    tmp = tempfile.mkdtemp(prefix="mms-standin-")
    results = asyncio.run(run(tmp))
    failed = [name for name, ok in results if not ok]
    print()
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    if failed or args.keep:
        print(f"kept: {tmp} (mmsd.log)")
    else:
        shutil.rmtree(tmp)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
