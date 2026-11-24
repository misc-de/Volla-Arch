# plinius-remote: the HID report map and report lengths, key layouts and what a character takes
# to type on the host, report pacing, the BlueZ peripheral's state machine (GATT, advertising,
# pairing agent) against a fake system bus, the history and the portrait keyboard, and the
# touchpad's gestures. GTK, Gdk, Gsk and Adw are faked; nothing reaches BlueZ.

import types

import pytest
from gi.repository import GLib

import desktop_fakes as fakes
from conftest import PKG

REMOTE = PKG / "plinius-remote" / "plinius-remote.py"


@pytest.fixture(scope="module")
def mod():
    import gi
    # The real Gtk 4 would load Graphene with its version first; with Gtk faked, do it here
    gi.require_version("Graphene", "1.0")
    return fakes.load_gui(REMOTE, "plinius_remote_under_test")


@pytest.fixture
def glib(mod, monkeypatch):
    fake = fakes.FakeGLib()
    monkeypatch.setattr(mod, "GLib", fake)
    return fake


# ----- the HID report map -----

def hid_reports(desc):
    """Bits per (report id, "input"/"output") of a HID report descriptor, and whether its
    collections close. Short items only, which is all the map uses."""
    bits, size, count, rid, depth, i = {}, 0, 0, 0, 0, 0
    while i < len(desc):
        prefix = desc[i]
        n = {0: 0, 1: 1, 2: 2, 3: 4}[prefix & 3]
        value = int.from_bytes(desc[i + 1:i + 1 + n], "little")
        tag = prefix & 0xFC
        i += 1 + n
        if tag == 0x74:
            size = value
        elif tag == 0x94:
            count = value
        elif tag == 0x84:
            rid = value
        elif tag in (0x80, 0x90):
            kind = "input" if tag == 0x80 else "output"
            bits[(rid, kind)] = bits.get((rid, kind), 0) + size * count
        elif tag == 0xA0:
            depth += 1
        elif tag == 0xC0:
            depth -= 1
    return bits, depth


def test_report_map_matches_the_report_lengths(mod):
    bits, depth = hid_reports(mod.REPORT_MAP)
    assert depth == 0
    assert {k: v // 8 for k, v in bits.items()} == mod.REPORT_LENGTHS
    assert all(v % 8 == 0 for v in bits.values())


def test_uuid16(mod):
    assert mod.uuid16(0x1812) == "00001812-0000-1000-8000-00805f9b34fb"


def test_error_text(mod):
    assert mod.error_text(GLib.Error("GDBus.Error:org.bluez.Error.Failed: Not ready")) == \
        "Not ready"
    assert mod.error_text(GLib.Error("Timeout was reached")) == "Timeout was reached"


@pytest.mark.parametrize("content, level", [("57\n", 57), ("100", 100), ("104", 100),
                                            ("-1", 0), ("full", 100), (None, 100)])
def test_battery_level(mod, tmp_path, monkeypatch, content, level):
    path = tmp_path / "capacity"
    if content is not None:
        path.write_text(content)
    monkeypatch.setattr(mod, "BATTERY", str(path))
    assert mod.battery_level() == level


# ----- layouts -----

@pytest.mark.parametrize("layout", ["us", "de"])
def test_full_keyboard_rows_are_16_units(mod, layout):
    rows = mod.keyboard_rows(layout)
    assert len(rows) == 6
    for row in rows:
        assert sum(width for _kind, _value, width in row) == pytest.approx(16)
    labels = mod.LABELS_DE if layout == "de" else mod.LABELS_US
    keys = [value for row in rows for kind, value, _w in row if kind == "key"]
    assert all(k in labels for k in keys)
    assert len(keys) == len(set(keys))


def test_german_rows_swap_y_and_z(mod):
    de = [v for row in mod.keyboard_rows("de") for k, v, _ in row if k == "key"]
    us = [v for row in mod.keyboard_rows("us") for k, v, _ in row if k == "key"]
    assert mod.LABELS_DE[0x1C] == "z" and mod.LABELS_DE[0x1D] == "y"
    assert 0x64 in de and 0x64 not in us and 0x31 in us and 0x32 in de


@pytest.mark.parametrize("layout, char, strokes", [
    ("us", "a", [(0x04, 0)]), ("us", "A", [(0x04, 0x02)]), ("us", "@", [(0x1F, 0x02)]),
    ("us", "1", [(0x1E, 0)]), ("us", " ", [(0x2C, 0)]), ("us", '"', [(0x34, 0x02)]),
    ("us", "~", [(0x35, 0x02)]), ("us", "|", [(0x31, 0x02)]),
    ("de", "z", [(0x1C, 0)]), ("de", "Y", [(0x1D, 0x02)]), ("de", "@", [(0x14, 0x40)]),
    ("de", "€", [(0x08, 0x40)]), ("de", "Ö", [(0x33, 0x02)]), ("de", "ß", [(0x2D, 0)]),
    ("de", "?", [(0x2D, 0x02)]), ("de", "\\", [(0x2D, 0x40)]), ("de", "|", [(0x64, 0x40)]),
    # Dead keys: the accent, then the letter; alone, then a space
    ("de", "é", [(0x2E, 0), (0x08, 0)]), ("de", "È", [(0x2E, 0x02), (0x08, 0x02)]),
    ("de", "Â", [(0x35, 0), (0x04, 0x02)]), ("de", "^", [(0x35, 0), (0x2C, 0)]),
    ("de", "`", [(0x2E, 0x02), (0x2C, 0)]),
])
def test_char_strokes(mod, layout, char, strokes):
    assert mod.char_strokes(layout)[char] == strokes


def test_characters_the_layout_cannot_type_are_missing(mod):
    us = mod.char_strokes("us")
    for char in "äöüß€§°é":
        assert char not in us


@pytest.mark.parametrize("layout", ["us", "de"])
def test_every_character_has_its_own_strokes(mod, layout):
    strokes = mod.char_strokes(layout)
    seen = {}
    for char, s in strokes.items():
        assert tuple(s) not in seen, f"{char!r} and {seen.get(tuple(s))!r}"
        seen[tuple(s)] = char


@pytest.mark.parametrize("layout", ["us", "de"])
def test_every_key_of_the_portrait_keyboard_types(mod, layout):
    strokes = mod.char_strokes(layout)
    for rows, extras in mod.phone_layers(layout).values():
        for row in rows:
            for char in row or "":
                assert char in strokes, char
        for char in extras:
            assert char in strokes
    de_letters = mod.phone_layers("de")["letters"][0]
    assert de_letters[0].startswith("qwertz") and de_letters[2].startswith("yx")


# ----- report pacing -----

@pytest.fixture
def reports(mod, glib):
    sent = []
    r = mod.Reports(lambda rid, data: sent.append((rid, bytes(data))))
    r.sent = sent
    return r


def drain(glib, reports):
    """Run the pacing timer until it stops by itself."""
    for _ in range(100):
        timers = glib.of_kind("timeout")
        if not timers:
            return
        glib.fire(timers[0])
    raise AssertionError("the timer never stopped")


def mouse(buttons=0, x=0, y=0, wheel=0, pan=0):
    return (2, bytes([buttons]) + x.to_bytes(2, "little", signed=True)
            + y.to_bytes(2, "little", signed=True) + wheel.to_bytes(1, "little", signed=True)
            + pan.to_bytes(1, "little", signed=True))


def test_first_report_goes_out_at_once_then_paced(mod, reports, glib):
    reports.move(3.6, -2.2)
    assert reports.sent == [mouse(x=3, y=-2)]
    (timer,) = glib.of_kind("timeout")
    assert glib.sources[timer][1] == mod.REPORT_INTERVAL_MS
    reports.move(0.5, 0.0)
    reports.move(0.5, 0.0)
    assert len(reports.sent) == 1  # coalesced until the timer
    glib.fire(timer)
    # The fractions add up: 0.6 + 1.0 = 1.6 -> 1, -0.2 stays
    assert reports.sent[1] == mouse(x=1)
    drain(glib, reports)
    assert glib.sources == {} and reports.timer == 0


def test_motion_is_clamped_and_the_rest_kept(mod, reports, glib):
    reports.move(40000, -40000)
    reports.scroll(300, -2.5)
    drain(glib, reports)
    assert reports.sent[0] == mouse(x=32767, y=-32767)
    assert reports.sent[1] == mouse(x=7233, y=-7233, wheel=127, pan=-2)
    assert reports.sent[2] == mouse(wheel=127)


def test_buttons_keep_their_order_with_motion(mod, reports, glib):
    reports.dx = 5.0  # collected motion, not sent yet
    reports.timer = 99  # a report just went out
    reports.click(1)
    assert reports.queue == [mouse(x=5), mouse(buttons=1), mouse()]
    reports.timer = 0  # the interval is over
    reports.kick()
    drain(glib, reports)
    assert reports.sent == [mouse(x=5), mouse(buttons=1), mouse()]


def test_keys(mod, reports, glib):
    reports.key(mod.MOD_SHIFT, 0x04)
    reports.keys(mod.MOD_CTRL, [1, 2, 3, 4, 5, 6, 7, 8])
    reports.consumer(mod.CC_AC_BACK)
    drain(glib, reports)
    assert reports.sent == [(1, bytes([2, 0, 4, 0, 0, 0, 0, 0])), (1, bytes(8)),
                            (1, bytes([1, 0, 1, 2, 3, 4, 5, 6])),
                            (3, b"\x24\x02"), (3, b"\x00\x00")]


def test_zoom_holds_ctrl_around_the_wheel(mod, reports, glib):
    reports.buttons = 1
    reports.zoom(-300)
    drain(glib, reports)
    assert reports.sent == [(1, bytes([1]) + bytes(7)), mouse(buttons=1, wheel=-127),
                            (1, bytes(8))]


def test_reset_drops_what_is_not_sent(mod, reports, glib):
    reports.timer = 99
    reports.key(0, 0x04)
    reports.move(5, 5)
    reports.buttons = 1
    reports.reset()
    assert reports.queue == [] and reports.buttons == 0 and not reports.pending_motion()


# ----- the peripheral against BlueZ -----

class Bus:
    """The system bus: asynchronous calls wait in .pending until answered."""

    def __init__(self):
        self.pending, self.emitted, self.sync = [], [], []
        self.registered, self.next_id = {}, 1
        self.sync_answers = {}

    def signal_subscribe(self, *args):
        return 1

    def call(self, name, path, iface, method, params, reply, flags, timeout, cancel, finish):
        self.pending.append(types.SimpleNamespace(
            path=path, iface=iface, method=method,
            params=params.unpack() if params is not None else None, finish=finish))

    def call_finish(self, res):
        if isinstance(res, Exception):
            raise res
        return res

    def answer(self, value=None, error=None):
        call = self.pending.pop(0)
        call.finish(self, error or value or GLib.Variant("()", ()))
        return call

    def run(self, fail=None):
        """Answer every call, also those the answers cause; fail={method: message}."""
        done = []
        while self.pending:
            method = self.pending[0].method
            err = (fail or {}).get(method)
            done.append(self.answer(error=GLib.Error(err) if err else None))
        return [(c.method, c.params) for c in done]

    def call_sync(self, name, path, iface, method, params, reply, flags, timeout, cancel):
        self.sync.append((path, method, params.unpack() if params is not None else None))
        answer = self.sync_answers.get(method)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def emit_signal(self, dest, path, iface, signal, params):
        self.emitted.append((path, signal, params.unpack()))

    def register_object_with_closures2(self, path, info, method, get, set_):
        self.registered[self.next_id] = path
        self.next_id += 1
        return self.next_id - 1

    def unregister_object(self, reg_id):
        del self.registered[reg_id]


ADAPTER = "/org/bluez/hci0"
HOST = "/org/bluez/hci0/dev_11_22_33_44_55_66"


def bluez_objects(powered=False, pairable=False, uuids=(), connected=False):
    v = GLib.Variant
    return v("(a{oa{sa{sv}}})", ({
        ADAPTER: {"org.bluez.Adapter1": {"Address": v("s", "AA:BB"), "Alias": v("s", "Volla"),
                                         "Powered": v("b", powered), "Pairable": v("b", pairable),
                                         "UUIDs": v("as", list(uuids))},
                  "org.bluez.GattManager1": {}, "org.bluez.LEAdvertisingManager1": {}},
        "/org/bluez/hci1": {"org.bluez.Adapter1": {"Address": v("s", "CC:DD")}},
        HOST: {"org.bluez.Device1": {"Alias": v("s", "Laptop"), "Connected": v("b", connected)}},
    },))


@pytest.fixture
def per(mod, monkeypatch, tmp_path):
    bus = Bus()
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(mod.Gio, bus_get_sync=lambda *a: bus,
                                                bus_watch_name_on_connection=lambda *a: 1))
    monkeypatch.setattr(mod, "BATTERY", str(tmp_path / "capacity"))
    (tmp_path / "capacity").write_text("80\n")
    ui = types.SimpleNamespace(changed=0, errors=[], pairs=[], cancels=0, new_hosts=[])

    def changed():
        ui.changed += 1

    def cancel():
        ui.cancels += 1
    p = mod.Peripheral(changed, ui.errors.append, lambda *a: ui.pairs.append(a), cancel,
                       set(), ui.new_hosts.append)
    p.bus_ = bus
    p.ui = ui
    p.tmp = tmp_path
    return p


def appear(p, **objects):
    p.on_bluez_appeared(None, "org.bluez", ":1.7")
    call = p.bus_.answer(bluez_objects(**objects))
    assert call.method == "GetManagedObjects"


def test_gatt_database(mod, per):
    hid, battery, devinfo = per.services
    uuids = [c.uuid[4:8] for c in hid.characteristics]
    assert uuids == ["2a4a", "2a4b", "2a4c", "2a4e", "2a4d", "2a4d", "2a4d", "2a4d"]
    refs = [(c.descriptors[0].value, c.flags) for c in hid.characteristics[4:]]
    assert [r[0] for r in refs] == [b"\x01\x01", b"\x02\x01", b"\x03\x01", b"\x01\x02"]
    # Reports and the map need an encrypted link
    assert all("encrypt-read" in f for _v, f in refs) and hid.characteristics[1].flags == \
        ["encrypt-read"]
    assert battery.characteristics[0].value == bytes([80])
    paths = [c.path for s in per.services for c in s.characteristics]
    assert len(paths) == len(set(paths))


def test_the_adapter_with_gatt_and_advertising_is_used(mod, per):
    appear(per)
    assert per.adapter == ADAPTER and per.available()
    assert per.devices == {HOST: {"Alias": "Laptop", "Connected": False}}
    assert per.device_name(HOST) == "Laptop"
    assert per.device_name("/org/bluez/hci0/dev_AA") == "AA"


def start(mod, per, fail=None, **objects):
    appear(per, **objects)
    per.start()
    return per.bus_.run(fail)


def test_start_registers_and_advertises(mod, per):
    calls = start(mod, per)
    assert [m for m, _ in calls] == ["Set", "Set", "RegisterAgent", "RequestDefaultAgent",
                                     "RegisterApplication", "RegisterAdvertisement"]
    assert calls[0][1] == ("org.bluez.Adapter1", "Powered", True)
    assert calls[1][1] == ("org.bluez.Adapter1", "Pairable", True)
    assert calls[2][1] == ("/org/plinius/remote/agent", "DisplayYesNo")
    assert per.state == per.VISIBLE and per.adv_registered
    assert per.app_registered and per.agent_registered
    assert per.name == "Volla Remote"
    # GATT tree with the Device Information service, the advertisement, the agent
    assert set(per.bus_.registered.values()) >= {"/org/plinius/remote",
                                                 "/org/plinius/remote/advertisement",
                                                 "/org/plinius/remote/devinfo"}


def test_start_on_a_ready_adapter_changes_no_setting(mod, per):
    calls = start(mod, per, powered=True, pairable=True, uuids=[mod.uuid16(0x180A)])
    assert [m for m, _ in calls][0] == "RegisterAgent"
    # BlueZ serves Device Information already
    assert "/org/plinius/remote/devinfo" not in per.bus_.registered.values()
    per.stop()
    stops = per.bus_.run()
    assert [m for m, _ in stops] == ["UnregisterAdvertisement", "UnregisterApplication",
                                     "UnregisterAgent"]
    assert per.state == per.OFF and per.bus_.registered == {}


def test_failed_start_undoes_what_was_done(mod, per):
    calls = start(mod, per, fail={"RequestDefaultAgent": "busy",
                                  "RegisterApplication": "GDBus.Error:org.bluez.Error.Failed: "
                                                         "No adapter"})
    methods = [m for m, _ in calls]
    # The default agent is optional, the application is not
    assert methods[:5] == ["Set", "Set", "RegisterAgent", "RequestDefaultAgent",
                           "RegisterApplication"]
    assert methods[5:] == ["UnregisterAgent", "Set"]
    assert calls[-1][1] == ("org.bluez.Adapter1", "Pairable", False)
    assert per.ui.errors == ["Could not start: RegisterApplication: No adapter"]
    assert per.state == per.OFF and per.bus_.registered == {}


def test_failing_advertisement_stops(mod, per):
    start(mod, per, fail={"RegisterAdvertisement": "no slot"}, powered=True, pairable=True)
    assert per.ui.errors == ["Could not become visible: no slot"]
    assert per.state == per.OFF


def method(per, iface, name, params, sender=":1.7", path="/org/plinius/remote"):
    inv = fakes.Invocation()
    per.on_method(None, sender, path, iface, name, params, inv)
    return inv


def test_only_bluez_may_call(mod, per):
    appear(per)
    inv = method(per, mod.OM_IFACE, "GetManagedObjects", GLib.Variant("()", ()), sender=":1.99")
    assert inv.error[0] == "org.bluez.Error.NotAuthorized"


def test_gatt_read_write_and_the_host(mod, per):
    start(mod, per, powered=True, pairable=True)
    report_map = per.services[0].characteristics[1]
    opts = GLib.Variant("(a{sv})", ({"offset": GLib.Variant("q", 10),
                                     "device": GLib.Variant("o", HOST)},))
    inv = method(per, mod.CHRC_IFACE, "ReadValue", opts, path=report_map.path)
    assert bytes(inv.value.unpack()[0]) == mod.REPORT_MAP[10:]
    assert per.host == HOST and per.known_hosts == {HOST} and per.ui.new_hosts == [HOST]
    leds = per.services[0].characteristics[7]
    inv = method(per, mod.CHRC_IFACE, "WriteValue",
                 GLib.Variant("(aya{sv})", (b"\x02", {})), path=leds.path)
    assert leds.value == b"\x02" and inv.answered == 1
    tree = method(per, mod.OM_IFACE, "GetManagedObjects", GLib.Variant("()", ())).value.unpack()[0]
    assert report_map.path in tree and tree[report_map.path][mod.CHRC_IFACE]["UUID"] == \
        mod.uuid16(0x2A4B)


def test_subscribing_host_ends_advertising(mod, per):
    start(mod, per, powered=True, pairable=True)
    keyboard = per.reports[mod.RID_KEYBOARD]
    mouse_ = per.reports[mod.RID_MOUSE]
    for chrc in (keyboard, mouse_, keyboard):
        method(per, mod.CHRC_IFACE, "StartNotify", GLib.Variant("()", ()), path=chrc.path)
    assert per.notify_count == 2 and per.state == per.CONNECTED
    assert [m for m, _ in per.bus_.run()] == ["UnregisterAdvertisement"]
    # Reports reach the host only now
    per.notify(mod.RID_KEYBOARD, bytes(8))
    assert per.bus_.emitted[-1][0] == keyboard.path
    for chrc in (keyboard, mouse_):
        method(per, mod.CHRC_IFACE, "StopNotify", GLib.Variant("()", ()), path=chrc.path)
    assert per.state == per.VISIBLE
    assert [m for m, _ in per.bus_.run()] == ["RegisterAdvertisement"]


def test_reports_wait_while_off(mod, per):
    per.notify(mod.RID_MOUSE, bytes(7))
    per.update_battery()
    assert per.bus_.emitted == []


def test_battery_change_is_sent(mod, per):
    start(mod, per, powered=True, pairable=True)
    (per.tmp / "capacity").write_text("79\n")
    per.update_battery()
    per.update_battery()
    assert per.bus_.emitted == [(per.battery.path, "PropertiesChanged",
                                 (mod.CHRC_IFACE, {"Value": [79]}, []))]


def test_known_host_reconnecting_counts_as_connected(mod, per):
    per.known_hosts.add(HOST)
    start(mod, per, powered=True, pairable=True)
    changed = GLib.Variant("(sa{sv}as)", (mod.DEVICE_IFACE, {"Connected": GLib.Variant("b", True)},
                                          []))
    per.on_props_changed(None, None, HOST, None, None, changed)
    assert per.host == HOST and per.state == per.CONNECTED
    per.bus_.run()
    gone = GLib.Variant("(sa{sv}as)", (mod.DEVICE_IFACE, {"Connected": GLib.Variant("b", False)},
                                       []))
    per.on_props_changed(None, None, HOST, None, None, gone)
    assert per.host is None and per.state == per.VISIBLE
    assert per.host_name() is None


def test_bluetooth_switched_off(mod, per):
    start(mod, per, powered=True, pairable=True)
    per.on_props_changed(None, None, ADAPTER, None, None, GLib.Variant(
        "(sa{sv}as)", (mod.ADAPTER_IFACE, {"Powered": GLib.Variant("b", False)}, [])))
    assert per.ui.errors == ["Bluetooth was switched off"] and per.state == per.STOPPING


def test_bluez_leaving(mod, per):
    start(mod, per, powered=True, pairable=True)
    per.on_bluez_vanished(None, None)
    assert per.state == per.OFF and per.adapter is None and per.bus_.registered == {}
    assert not (per.app_registered or per.agent_registered or per.adv_registered)


def test_stop_when_quitting_is_synchronous(mod, per):
    start(mod, per)
    per.stop(sync=True)
    assert [c[1] for c in per.bus_.sync] == ["UnregisterAdvertisement", "UnregisterApplication",
                                            "UnregisterAgent", "Set"]
    assert per.state == per.OFF and per.bus_.pending == []


@pytest.mark.parametrize("alias, name", [
    ("Volla", "Volla Remote"),
    ("Volla Phone 22 of Anna", "Volla Phone Remote"),  # 18 bytes at most
    ("Völlä Phöne", "Völlä Ph Remote"),
    ("Volla Phone  X", "Volla Phone Remote"),
    ("        ", "Plinius Remote"),
])
def test_local_name_fits_the_advertisement(mod, per, alias, name):
    per.adapter_props = {"Alias": alias}
    assert per.local_name() == name
    assert len(per.local_name().encode()) <= mod.MAX_NAME_BYTES


# ----- pairing agent -----

def agent(per, name, *args, sig=None):
    params = GLib.Variant(sig, args) if sig else GLib.Variant("()", ())
    return method(per, "org.bluez.Agent1", name, params, path="/org/plinius/remote/agent")


def test_confirmation_waits_for_the_user(mod, per):
    appear(per)
    inv = agent(per, "RequestConfirmation", HOST, 1234, sig="(ou)")
    assert inv.answered == 0
    ((kind, name, code, answer),) = per.ui.pairs
    assert (kind, name, code) == ("confirm", "Laptop", "001234")
    answer(True)
    answer(False)  # a second answer is ignored
    assert inv.answered == 1 and inv.error is None


def test_rejected_and_cancelled_questions(mod, per):
    appear(per)
    first = agent(per, "RequestAuthorization", HOST, sig="(o)")
    second = agent(per, "RequestConfirmation", HOST, 999999, sig="(ou)")
    # The newer question replaces the open one
    assert first.error == ("org.bluez.Error.Canceled", "Superseded") and per.ui.cancels == 1
    per.ui.pairs[0][3](True)
    assert first.answered == 1
    agent(per, "Cancel")
    assert per.ui.cancels == 2
    per.ui.pairs[1][3](True)
    assert second.answered == 0  # cancelled: the late answer goes nowhere


def test_display_passkey_is_answered_at_once(mod, per):
    appear(per)
    inv = agent(per, "DisplayPasskey", HOST, 42, 0, sig="(ouq)")
    assert inv.answered == 1 and per.ui.pairs[0][:3] == ("display", "Laptop", "000042")
    assert per.pending_agent is None


@pytest.mark.parametrize("name, sig, args", [("RequestPinCode", "(o)", (HOST,)),
                                             ("RequestPasskey", "(o)", (HOST,))])
def test_no_typing_on_this_side(mod, per, name, sig, args):
    appear(per)
    assert agent(per, name, *args, sig=sig).error[0] == "org.bluez.Error.Rejected"


@pytest.mark.parametrize("paired, ok", [(True, True), (False, False), (None, False)])
def test_authorize_service_only_for_paired_devices(mod, per, paired, ok):
    appear(per)
    per.bus_.sync_answers["Get"] = (GLib.Error("gone") if paired is None
                                    else GLib.Variant("(v)", (GLib.Variant("b", paired),)))
    inv = agent(per, "AuthorizeService", HOST, "0000110a-0000-1000-8000-00805f9b34fb",
                sig="(os)")
    assert (inv.error is None) is ok and inv.answered == 1


# ----- history of what was typed -----

class Row:
    def __init__(self, *a, **k):
        self.tooltip = None

    def set_child(self, child):
        pass

    def set_tooltip_text(self, text):
        self.tooltip = text

    def get_tooltip_text(self):
        return self.tooltip

    def set_visible(self, visible):
        self.visible = visible


@pytest.fixture
def history(mod, glib, monkeypatch):
    monkeypatch.setattr(mod.Gtk, "ListBoxRow", Row)
    replayed, changes = [], []
    h = mod.InputHistory(replayed.append, lambda: changes.append(1))
    h.replayed = replayed
    return h


def entries(h):
    return [(row.get_tooltip_text(), strokes) for row, strokes in h.entries]


def test_history_entries(mod, history):
    h = history
    for ch, usage in (("h", 0x0B), ("i", 0x0C), ("x", 0x1B)):
        h.add(ch, [(usage, 0)])
    h.backspace()
    h.add("⏎", [(0x28, 0)])
    h.close()
    assert entries(h) == [("hi⏎", [(0x0B, 0), (0x0C, 0), (0x28, 0)])]
    h.shortcut("Ctrl+C", [(0x06, 1)])
    h.add("a", [(0x04, 0)])
    h.shortcut("Ctrl+C", [(0x06, 1)])  # typed again: the older copy goes
    assert [e[0] for e in entries(h)] == ["hi⏎", "a", "Ctrl+C"]
    assert not h.empty()


def test_history_replay_moves_the_entry_last(mod, history):
    h = history
    h.shortcut("one", [(1, 0)])
    h.shortcut("two", [(2, 0)])
    h.add("p", [(0x13, 0)])
    first_row = h.entries[0][0]
    h.on_row_activated(None, first_row)
    # What was being typed is closed first
    assert [e[0] for e in entries(h)] == ["two", "p", "one"]
    assert h.replayed == [[(1, 0)]]
    h.on_row_activated(None, Row())  # not an entry
    assert len(h.replayed) == 1


def test_history_is_bounded_and_clears(mod, history):
    h = history
    for i in range(mod.InputHistory.MAX_ENTRIES + 5):
        h.shortcut(str(i), [(i, 0)])
    assert len(h.entries) == mod.InputHistory.MAX_ENTRIES
    assert entries(h)[0][0] == "5"
    h.add("x", [(0x1B, 0)])
    h.clear()
    assert h.empty()


# ----- the portrait keyboard -----

class Toggle:
    def __init__(self, *a, on_toggled=None, **k):
        self.active = False
        self.on_toggled = on_toggled

    def get_active(self):
        return self.active

    def set_active(self, active):
        changed = active != self.active
        self.active = active
        if changed and self.on_toggled:
            self.on_toggled(self)

    def set_label(self, label):
        self.label = label


class Label:
    def set_label(self, text):
        self.text = text

    def add_css_class(self, css):
        pass


class Key:
    def __init__(self, label, css, down, up):
        self.label, self.css, self.down, self.up = label, css, down, up
        self.child = Label()

    def get_first_child(self):
        return self.child

    def add_css_class(self, css):
        pass

    def remove_css_class(self, css):
        pass

    def tap(self):
        self.down(self)
        if self.up:
            self.up(self)


class KeyReports:
    def __init__(self):
        self.sent = []

    def key(self, mods, usage):
        self.sent.append(("key", mods, usage))

    def keys(self, mods, usages):
        self.sent.append(("keys", mods, list(usages)))

    def consumer(self, usage):
        self.sent.append(("consumer", usage))


class History:
    def __init__(self):
        self.log = []

    def __getattr__(self, name):
        return lambda *args: self.log.append((name,) + args)


@pytest.fixture
def keyboard(mod, glib):
    def make(layout="us"):
        kb = mod.PhoneKeyboard.__new__(mod.PhoneKeyboard)
        kb.reports = KeyReports()
        kb.get_reports = lambda: kb.reports
        kb.history = History()
        kb.layout, kb.strokes = None, {}
        kb.shift, kb.shift_time, kb.shift_key = kb.SHIFT_OFF, 0, None
        kb.letter_labels, kb.held, kb.pending = [], [], None
        kb.long_press, kb.popover = 0, None
        kb.modifiers = {mod.MOD_CTRL: Toggle(), mod.MOD_ALT: Toggle()}
        kb.nav_keys = {}
        kb.layers = fakes.Stub()
        kb.made = []

        def make_key(label, css, down, up=None):
            key = Key(label, css, down, up)
            kb.made.append(key)
            return key
        kb.make_key = make_key
        kb.place_popover = lambda *a: None
        kb.set_layout(layout)
        return kb
    return make


def key(kb, label, layer_index=0):
    found = [k for k in kb.made if k.label == label]
    return found[layer_index]


def test_typing_letters_and_shift(mod, keyboard, glib):
    kb = keyboard("us")
    key(kb, "h").tap()
    kb.on_shift(None)
    key(kb, "i").tap()
    key(kb, "j").tap()  # shift was for one letter
    assert kb.reports.sent == [("key", 0, 0x0B), ("key", 2, 0x0C), ("key", 0, 0x0D)]
    assert [e[1] for e in kb.history.log] == ["h", "I", "j"]


def test_double_shift_locks(mod, keyboard, glib):
    kb = keyboard("us")
    kb.on_shift(None)
    glib.advance_ms(100)
    kb.on_shift(None)
    assert kb.shift == kb.SHIFT_LOCKED
    key(kb, "q").tap()
    key(kb, "w").tap()
    assert [s[1] for s in kb.reports.sent] == [2, 2]
    kb.on_shift(None)
    assert kb.shift == kb.SHIFT_OFF
    # Two taps too far apart are on and off
    kb.on_shift(None)
    glib.advance_ms(1000)
    kb.on_shift(None)
    assert kb.shift == kb.SHIFT_OFF


def test_ctrl_goes_with_the_last_stroke_only(mod, keyboard, glib):
    kb = keyboard("de")
    kb.modifiers[mod.MOD_CTRL].active = True
    kb.type("é")
    assert kb.reports.sent == [("key", 0, 0x2E), ("key", mod.MOD_CTRL, 0x08)]
    assert kb.history.log == [("shortcut", "Strg+É", [(0x2E, 0), (0x08, mod.MOD_CTRL)])]
    assert not kb.modifiers[mod.MOD_CTRL].active


def test_keys_with_alternates_wait_for_the_finger(mod, keyboard, glib):
    kb = keyboard("de")
    a = key(kb, "a")
    a.down(a)
    assert kb.reports.sent == [] and kb.pending
    (timer,) = glib.of_kind("timeout")
    assert glib.sources[timer][1] == mod.LONG_PRESS_MS
    # Touching another key first types the waiting one, in order
    b = key(kb, "b")
    b.down(b)
    a.up(a)
    assert [s[2] for s in kb.reports.sent] == [0x04, 0x05] and timer in glib.removed


def test_long_press_types_nothing_itself(mod, keyboard, glib):
    kb = keyboard("de")
    e = key(kb, "e")
    e.down(e)
    (timer,) = glib.of_kind("timeout")
    glib.fire(timer)
    e.up(e)
    assert kb.reports.sent == [] and kb.pending is None


def test_held_keys_and_the_history(mod, keyboard, glib):
    kb = keyboard("us")
    backspace = key(kb, "⌫")
    enter = key(kb, "⏎")
    backspace.down(backspace)
    enter.down(enter)
    enter.up(enter)
    backspace.up(backspace)
    assert kb.reports.sent == [("keys", 0, [0x2A]), ("keys", 0, [0x2A, 0x28]),
                               ("keys", 0, [0x2A]), ("keys", 0, [])]
    assert [e[0] for e in kb.history.log] == ["backspace", "add", "close"]
    kb.modifiers[mod.MOD_ALT].active = True
    tab_space = key(kb, "")  # the space bar's label is the layout name, set afterwards
    tab_space.tap()
    assert kb.reports.sent[-2] == ("keys", mod.MOD_ALT, [0x2C])
    assert kb.history.log[-1] == ("shortcut", "Alt+␣", [(0x2C, mod.MOD_ALT)])


def test_release_lets_go_of_held_keys(mod, keyboard, glib):
    kb = keyboard("us")
    space = key(kb, "")
    space.down(space)
    kb.release()
    assert kb.reports.sent[-1] == ("keys", 0, []) and kb.held == []


def test_unknown_layout_labels(mod, keyboard):
    kb = keyboard("de")
    assert kb.label(mod.KEY_DELETE) == "Entf" and kb.label(0x99) == "?"
    assert kb.combo_label(mod.MOD_CTRL | mod.MOD_ALT, "Entf") == "Strg+Alt+Entf"
    us = keyboard("us")
    assert us.combo_label(mod.MOD_CTRL, "c") == "Ctrl+C"


# ----- the landscape layout -----

class Child:
    def __init__(self, height):
        self.height = height
        self.visible = None
        self.allocated = None

    def measure(self, orientation, for_size):
        return 0, self.height, -1, -1

    def set_child_visible(self, visible):
        self.visible = visible

    def allocate(self, w, h, baseline, transform):
        self.allocated = (w, h)


@pytest.mark.parametrize("size, media_shown, kb_size", [
    ((800, 400), True, (788, 388 - 50 - 6)),      # landscape
    ((400, 800), True, (788, 388 - 50 - 6)),      # portrait: turned, the same room
    ((800, 300), False, (788, 288)),              # too low for both: the keyboard only
])
def test_landscape_keyboard_layout(mod, size, media_shown, kb_size):
    lk = mod.LandscapeKeyboard.__new__(mod.LandscapeKeyboard)
    lk.media, lk.keyboard = Child(50), Child(240)
    lk.do_size_allocate(*size, -1)
    assert lk.media.visible is media_shown
    assert lk.keyboard.allocated == kb_size
    assert (lk.media.allocated is not None) is media_shown


# ----- the touchpad -----

class Gesture:
    def __init__(self, center=(100.0, 100.0), scale=1.0):
        self.center, self.scale = center, scale

    def set_state(self, state):
        pass

    def get_bounding_box_center(self):
        return (True, *self.center)

    def get_scale_delta(self):
        return self.scale


@pytest.fixture
def pad(mod, glib, monkeypatch):
    monkeypatch.setattr(mod, "default_layout", lambda: "us")
    app = mod.RemoteApp()
    sent = []
    app.reports = mod.Reports(lambda rid, data: sent.append((rid, bytes(data))))
    app.sent = sent
    app.pad = fakes.Stub()
    app.hold_toggle = Toggle(on_toggled=app.on_hold_toggled)
    app.glib = glib
    return app


def flush(app):
    for _ in range(200):
        timers = [s for s in app.glib.of_kind("timeout")
                  if app.glib.sources[s][2] == app.reports.on_timer]
        if not timers:
            return app.sent
        app.glib.fire(timers[0])
    raise AssertionError("reports never drained")


def buttons(sent):
    return [data[0] for rid, data in sent if rid == 2 and data[1:5] == bytes(4)]


def test_tap_clicks(mod, pad):
    pad.on_drag_begin(Gesture(), 0, 0)
    pad.glib.advance_ms(80)
    pad.on_drag_update(None, 2, 1)
    pad.on_drag_end(None, 2, 1)
    sent = flush(pad)
    assert buttons(sent) == [1, 0]


def test_slow_move_is_precise_and_fast_move_goes_far(mod, pad):
    pad.on_drag_begin(Gesture(), 0, 0)
    pad.glib.advance_ms(100)
    pad.on_drag_update(None, 10, 0)  # 0.1 px/ms: below the threshold
    assert pad.reports.dx + sum(int.from_bytes(d[1:3], "little", signed=True)
                                for r, d in pad.sent) == pytest.approx(16.0)
    pad.glib.advance_ms(10)
    pad.on_drag_update(None, 110, 0)  # 10 px/ms: the gain is capped
    total = pad.reports.dx + sum(int.from_bytes(d[1:3], "little", signed=True)
                                 for r, d in pad.sent)
    assert total == pytest.approx(16.0 + 100 * 1.6 * (1 + 1.6 * 2.5))
    pad.glib.advance_ms(300)
    pad.on_drag_end(None, 110, 0)
    assert buttons(flush(pad)) == []  # moved: no click


def test_long_press_holds_the_button_until_lifted(mod, pad):
    pad.on_drag_begin(Gesture(), 0, 0)
    (lp,) = [s for s in pad.glib.of_kind("timeout")
             if pad.glib.sources[s][1] == mod.LONG_PRESS_MS]
    pad.glib.advance_ms(mod.LONG_PRESS_MS)
    pad.glib.fire(lp)
    assert pad.holding and pad.reports.buttons == 1
    pad.on_drag_update(None, 50, 0)
    pad.glib.advance_ms(500)
    pad.on_drag_end(None, 50, 0)
    assert pad.reports.buttons == 0 and not pad.holding
    assert buttons(flush(pad)) == [1, 0]


def test_tap_and_drag(mod, pad):
    pad.on_drag_begin(Gesture(), 0, 0)
    pad.glib.advance_ms(50)
    pad.on_drag_end(None, 0, 0)
    pad.glib.advance_ms(100)  # touching again within DOUBLE_TAP_MS
    pad.on_drag_begin(Gesture(), 0, 0)
    assert pad.reports.buttons == 1 and pad.tap_drag
    pad.glib.advance_ms(400)
    pad.on_drag_update(None, 40, 0)
    pad.on_drag_end(None, 40, 0)
    assert pad.reports.buttons == 0
    assert buttons(flush(pad)) == [1, 0, 1, 0]


def test_two_finger_scroll_follows_the_fingers(mod, pad):
    g = Gesture(center=(100.0, 100.0))
    pad.on_two_begin(g, None)
    g.center = (100.0, 144.0)  # fingers move down 44 px
    pad.on_two_update(g, None)
    assert pad.zoom_mode == "scroll"
    assert pad.reports.wheel + sum(d[5] for r, d in pad.sent) == pytest.approx(2.0)
    pad.natural = False
    g.center = (100.0, 166.0)
    pad.on_two_update(g, None)
    flush(pad)
    assert [int.from_bytes(d[5:6], "little", signed=True) for r, d in pad.sent] == [2, -1]


def test_pinch_zooms(mod, pad):
    g = Gesture(scale=1.0)
    pad.on_two_begin(g, None)
    g.scale = 1.3  # spread: two zoom steps of 12 %
    pad.on_two_update(g, None)
    assert pad.zoom_mode == "zoom"
    sent = flush(pad)
    assert sent[0] == (1, bytes([mod.MOD_CTRL]) + bytes(7))
    assert sent[1][1][5] == 2
    g.scale = 0.97  # pinched back past the start: two steps out
    pad.on_two_update(g, None)
    assert flush(pad)[4][1][5] == (-2) & 0xFF


def test_two_finger_tap_is_a_right_click(mod, pad):
    g = Gesture()
    pad.on_two_begin(g, None)
    pad.glib.advance_ms(100)
    pad.on_two_end(g, None)
    assert buttons(flush(pad)) == [2, 0]


class Touch:
    def __init__(self, kind, seq, x, y):
        self.kind, self.seq, self.pos = kind, seq, (x, y)

    def get_event_type(self):
        return self.kind

    def get_event_sequence(self):
        return self.seq

    def get_position(self):
        return (True, *self.pos)


def touches(pad, mod, kind, points):
    E = mod.Gdk.EventType
    for seq, (x, y) in points.items():
        pad.on_pad_event(None, Touch(getattr(E, kind), seq, x, y))


def test_three_finger_swipe_switches_windows(mod, pad):
    start = {1: (100, 300), 2: (150, 300), 3: (200, 300)}
    touches(pad, mod, "TOUCH_BEGIN", start)
    touches(pad, mod, "TOUCH_UPDATE", {s: (x + 200, y) for s, (x, y) in start.items()})
    touches(pad, mod, "TOUCH_END", start)
    keyboard = [d for r, d in flush(pad) if r == 1]
    alt_tab = bytes([mod.MOD_ALT, 0, mod.KEY_TAB, 0, 0, 0, 0, 0])
    alt = bytes([mod.MOD_ALT]) + bytes(7)
    # The centroid moved 200 px: two windows on (one step per 90 px), then Alt goes up
    assert keyboard[-5:] == [alt_tab, alt, alt_tab, alt, bytes(8)]


def test_three_finger_swipe_up_opens_the_overview(mod, pad):
    start = {1: (100, 300), 2: (150, 300), 3: (200, 300)}
    touches(pad, mod, "TOUCH_BEGIN", start)
    touches(pad, mod, "TOUCH_UPDATE", {s: (x, y - 120) for s, (x, y) in start.items()})
    touches(pad, mod, "TOUCH_END", start)
    keyboard = [d for r, d in flush(pad) if r == 1]
    assert keyboard == [bytes([mod.MOD_SUPER]) + bytes(7), bytes(8)]


def test_three_finger_tap_is_a_middle_click(mod, pad):
    start = {1: (100, 300), 2: (150, 300), 3: (200, 300)}
    touches(pad, mod, "TOUCH_BEGIN", start)
    pad.glib.advance_ms(100)
    touches(pad, mod, "TOUCH_END", start)
    assert buttons(flush(pad)) == [4, 0]


# ----- settings -----

def test_settings_round_trip(mod, pad, tmp_path, monkeypatch):
    path = tmp_path / "config" / "plinius-remote" / "settings.ini"
    monkeypatch.setattr(mod, "SETTINGS_FILE", str(path))
    pad.speed, pad.natural, pad.layout = 2.25, False, "de"
    pad.known_hosts = {HOST, "/org/bluez/hci0/dev_00"}
    pad.save_settings()
    other = mod.RemoteApp()
    other.load_settings()
    assert (other.speed, other.natural, other.layout) == (2.25, False, "de")
    assert other.known_hosts == pad.known_hosts


def test_settings_partly_missing(mod, pad, tmp_path, monkeypatch):
    path = tmp_path / "settings.ini"
    path.write_text("[touchpad]\nspeed=abc\nnatural-scrolling=false\n")
    monkeypatch.setattr(mod, "SETTINGS_FILE", str(path))
    pad.load_settings()
    assert (pad.speed, pad.natural, pad.layout, pad.known_hosts) == (1.6, False, "us", set())
    monkeypatch.setattr(mod, "SETTINGS_FILE", str(tmp_path / "none.ini"))
    pad.load_settings()
    assert pad.speed == 1.6


# ----- the peripheral: less common answers and signals from BlueZ -----

def test_host_leds_are_logged(mod, per, capsys):
    start(mod, per, powered=True, pairable=True)
    leds = per.services[0].characteristics[7]
    method(per, mod.CHRC_IFACE, "WriteValue", GLib.Variant("(aya{sv})", (b"\x05", {})),
           path=leds.path)
    method(per, mod.CHRC_IFACE, "WriteValue", GLib.Variant("(aya{sv})", (b"", {})),
           path=leds.path)
    lines = [ln for ln in capsys.readouterr().err.splitlines() if "LEDs" in ln]
    assert lines == ["plinius-remote: host LEDs: num 1, caps 0, scroll 1"]


def test_write_at_an_offset_and_without_a_handler(mod, per):
    start(mod, per, powered=True, pairable=True)
    mode = per.protocol_mode
    inv = method(per, mod.CHRC_IFACE, "WriteValue", GLib.Variant(
        "(aya{sv})", (b"\x00", {"offset": GLib.Variant("q", 1)})), path=mode.path)
    assert mode.value == b"\x01\x00" and inv.answered == 1 and inv.error is None


def test_battery_change_while_off_is_kept_for_later(mod, per):
    (per.tmp / "capacity").write_text("50\n")
    per.update_battery()
    assert per.battery.value == bytes([50]) and per.bus_.emitted == []


def test_calls_without_a_callback(mod, per):
    per.call("/", mod.OM_IFACE, "GetManagedObjects")
    per.call("/", mod.OM_IFACE, "GetManagedObjects")
    per.bus_.answer(error=GLib.Error("gone"))
    per.bus_.answer()
    assert per.bus_.pending == [] and per.ui.errors == []


def test_bluez_objects_unreadable(mod, per, capsys):
    per.on_bluez_appeared(None, "org.bluez", ":1.7")
    per.bus_.answer(error=GLib.Error("Timeout was reached"))
    assert per.adapter is None and per.ui.changed == 0
    assert "reading BlueZ objects failed: Timeout was reached" in capsys.readouterr().err


def test_bluez_vanishing_while_off(mod, per, capsys):
    per.on_bluez_vanished(None, None)  # never there: nothing to say
    assert "BlueZ left the bus" not in capsys.readouterr().err and per.ui.changed == 1
    appear(per)
    per.on_bluez_vanished(None, None)
    assert "BlueZ left the bus" in capsys.readouterr().err
    assert per.state == per.OFF and not per.available() and per.devices == {}


def interfaces(signal_name, path, ifaces):
    sig = "(oa{sa{sv}})" if signal_name == "InterfacesAdded" else "(oas)"
    return GLib.Variant(sig, (path, ifaces))


def test_adapters_and_devices_coming_and_going(mod, per):
    per.on_bluez_appeared(None, "org.bluez", ":1.7")
    per.bus_.answer(GLib.Variant("(a{oa{sa{sv}}})", ({},)))
    assert not per.available()
    v = GLib.Variant
    adapter = {"org.bluez.Adapter1": {"Address": v("s", "AA:BB")},
               "org.bluez.GattManager1": {}, "org.bluez.LEAdvertisingManager1": {}}
    per.on_objects_changed(None, None, "/", None, "InterfacesAdded",
                           interfaces("InterfacesAdded", ADAPTER, adapter))
    assert per.adapter == ADAPTER and per.ui.changed == 2
    # A device without an alias goes by its address
    per.on_objects_changed(None, None, "/", None, "InterfacesAdded", interfaces(
        "InterfacesAdded", HOST, {"org.bluez.Device1": {"Address": v("s", "11:22")}}))
    assert per.devices[HOST] == {"Alias": "11:22", "Connected": False}
    per.on_objects_changed(None, None, "/", None, "InterfacesRemoved",
                           interfaces("InterfacesRemoved", HOST, [mod.DEVICE_IFACE]))
    assert HOST not in per.devices
    # Another adapter going does not matter; ours does
    per.on_objects_changed(None, None, "/", None, "InterfacesRemoved",
                           interfaces("InterfacesRemoved", "/org/bluez/hci1",
                                      [mod.ADAPTER_IFACE]))
    assert per.available()
    per.on_objects_changed(None, None, "/", None, "SomethingElse", None)
    per.on_objects_changed(None, None, "/", None, "InterfacesRemoved",
                           interfaces("InterfacesRemoved", ADAPTER, [mod.ADAPTER_IFACE]))
    assert not per.available()


def props(iface, **changed):
    return GLib.Variant("(sa{sv}as)", (iface, changed, []))


def test_property_changes_that_change_nothing_else(mod, per):
    appear(per)
    v = GLib.Variant
    per.on_props_changed(None, None, ADAPTER, None, None,
                         props(mod.ADAPTER_IFACE, Alias=v("s", "Anna's Volla")))
    assert per.adapter_props["Alias"] == "Anna's Volla" and per.ui.errors == []
    # Switched off while we are off: no complaint
    per.on_props_changed(None, None, ADAPTER, None, None,
                         props(mod.ADAPTER_IFACE, Powered=v("b", False)))
    assert per.ui.errors == []
    per.on_props_changed(None, None, HOST, None, None,
                         props(mod.DEVICE_IFACE, Alias=v("s", "Work laptop")))
    assert per.devices[HOST] == {"Alias": "Work laptop", "Connected": False}
    # Unknown devices and other interfaces are not tracked
    per.on_props_changed(None, None, "/org/bluez/hci0/dev_99", None, None,
                         props(mod.DEVICE_IFACE, Connected=v("b", True)))
    assert "/org/bluez/hci0/dev_99" not in per.devices


def test_other_devices_connecting_are_not_the_host(mod, per):
    start(mod, per, powered=True, pairable=True)
    v = GLib.Variant
    per.on_props_changed(None, None, HOST, None, None,
                         props(mod.DEVICE_IFACE, Connected=v("b", True)))
    assert per.host is None and per.state == per.VISIBLE  # not a known host
    per.on_props_changed(None, None, HOST, None, None,
                         props(mod.DEVICE_IFACE, Connected=v("b", False)))
    assert per.state == per.VISIBLE


def test_host_that_read_and_left_while_visible(mod, per):
    per.known_hosts.add(HOST)
    start(mod, per, powered=True, pairable=True)
    opts = GLib.Variant("(a{sv})", ({"device": GLib.Variant("o", HOST)},))
    method(per, mod.CHRC_IFACE, "ReadValue", opts, path=per.services[0].characteristics[0].path)
    # Known already: not stored again; not subscribed and not connected: still visible
    assert per.host == HOST and per.ui.new_hosts == [] and per.state == per.VISIBLE
    per.on_props_changed(None, None, HOST, None, None,
                         props(mod.DEVICE_IFACE, Connected=GLib.Variant("b", False)))
    assert per.host is None and per.state == per.VISIBLE


def test_advertisement_and_object_properties(mod, per):
    start(mod, per, powered=True, pairable=True)
    get = per.on_get_property
    assert get(None, ":1.7", mod.ADV_PATH, mod.ADV_IFACE, "LocalName").unpack() == \
        "Volla Remote"
    assert get(None, ":1.7", mod.ADV_PATH, mod.ADV_IFACE, "ServiceUUIDs").unpack() == \
        [mod.uuid16(0x1812)]
    assert get(None, ":1.7", mod.ADV_PATH, mod.ADV_IFACE, "Appearance").unpack() == 0x03C0
    chrc = per.battery
    assert get(None, ":1.7", chrc.path, mod.CHRC_IFACE, "Flags").unpack() == ["read", "notify"]
    desc = per.reports[mod.RID_MOUSE].descriptors[0]
    assert get(None, ":1.7", desc.path, mod.DESC_IFACE, "Characteristic").unpack() == \
        per.reports[mod.RID_MOUSE].path


def test_advertisement_released_by_bluez(mod, per):
    start(mod, per, powered=True, pairable=True)
    inv = method(per, mod.ADV_IFACE, "Release", GLib.Variant("()", ()), path=mod.ADV_PATH)
    assert inv.answered == 1 and not per.adv_registered
    per.stop()
    # Nothing to unregister for the advertisement any more
    assert [m for m, _ in per.bus_.run()] == ["UnregisterApplication", "UnregisterAgent"]


def test_notifications_of_other_characteristics(mod, per):
    start(mod, per, powered=True, pairable=True)
    battery = per.battery
    stop = method(per, mod.CHRC_IFACE, "StopNotify", GLib.Variant("()", ()), path=battery.path)
    assert stop.answered == 1 and not battery.notifying  # was not on
    method(per, mod.CHRC_IFACE, "StartNotify", GLib.Variant("()", ()), path=battery.path)
    assert battery.notifying and per.notify_count == 0 and per.state == per.VISIBLE
    method(per, mod.CHRC_IFACE, "StopNotify", GLib.Variant("()", ()), path=battery.path)
    assert not battery.notifying and per.state == per.VISIBLE


def test_unsupported_gatt_method(mod, per):
    start(mod, per, powered=True, pairable=True)
    inv = method(per, mod.CHRC_IFACE, "AcquireWrite", GLib.Variant("()", ()),
                 path=per.battery.path)
    assert inv.error == ("org.bluez.Error.NotSupported", "AcquireWrite")


def test_subscribing_while_off_changes_no_state(mod, per):
    appear(per)
    per.export()
    method(per, mod.CHRC_IFACE, "StartNotify", GLib.Variant("()", ()),
           path=per.reports[mod.RID_MOUSE].path)
    assert per.notify_count == 1 and per.state == per.OFF and per.bus_.pending == []


def test_agent_release_cancel_and_unknown(mod, per):
    appear(per)
    per.agent_registered = True
    assert agent(per, "Release").answered == 1 and not per.agent_registered
    assert agent(per, "Cancel").answered == 1 and per.ui.cancels == 0  # nothing open
    assert agent(per, "Frobnicate").error == ("org.bluez.Error.Rejected", "Frobnicate")


def test_rejected_by_the_user(mod, per):
    appear(per)
    inv = agent(per, "RequestConfirmation", HOST, 1, sig="(ou)")
    per.ui.pairs[0][3](False)
    assert inv.error == ("org.bluez.Error.Rejected", "Rejected by the user")


def test_start_only_once_and_only_with_an_adapter(mod, per):
    per.start()
    assert per.state == per.OFF and per.bus_.pending == []
    start(mod, per, powered=True, pairable=True)
    per.start()
    assert per.bus_.pending == [] and per.state == per.VISIBLE


def test_stop_when_off_does_nothing(mod, per):
    appear(per)
    per.stop()
    assert per.bus_.pending == [] and per.ui.changed == 1


def test_stop_cancels_an_open_question(mod, per):
    start(mod, per, powered=True, pairable=True)
    inv = agent(per, "RequestAuthorization", HOST, sig="(o)")
    per.stop()
    assert inv.error == ("org.bluez.Error.Canceled", "Stopped") and per.ui.cancels == 1
    assert per.pending_agent is None


def test_stop_while_starting_puts_pairable_back(mod, per):
    appear(per)
    per.start()
    assert per.bus_.pending[0].method == "Set"  # Powered, not answered yet
    per.stop()
    # Nothing registered yet: only the Pairable setting goes back
    assert per.state == per.STOPPING
    assert [(c.method, c.params) for c in per.bus_.pending[1:]] == [
        ("Set", ("org.bluez.Adapter1", "Pairable", False))]


def test_quick_on_and_off_leaves_nothing_registered(mod, per):
    appear(per, powered=True, pairable=True)
    per.start()
    per.stop()  # before BlueZ answered RegisterAgent
    start_calls = per.bus_.run()
    registered = set()
    for m, _params in start_calls:
        if m.startswith("Register"):
            registered.add(m[len("Register"):])
        elif m.startswith("Unregister"):
            registered.discard(m[len("Unregister"):])
    assert per.state == per.OFF
    assert registered == set() and not per.agent_registered and not per.app_registered
    # The step after the agent never runs; the agent goes right after its registration
    assert [m for m, _ in start_calls] == ["RegisterAgent", "UnregisterAgent"]
    assert per.bus_.registered == {}
    # A start after that works from the beginning
    per.start()
    assert [m for m, _ in per.bus_.run()] == ["RegisterAgent", "RequestDefaultAgent",
                                             "RegisterApplication", "RegisterAdvertisement"]
    assert per.state == per.VISIBLE and per.agent_registered and per.app_registered


def test_stop_while_registering_the_application(mod, per):
    appear(per, powered=True, pairable=True)
    per.start()
    for method in ("RegisterAgent", "RequestDefaultAgent"):
        assert per.bus_.answer().method == method
    assert per.bus_.pending[0].method == "RegisterApplication"
    per.stop()
    calls = per.bus_.run()
    assert [m for m, _ in calls] == ["RegisterApplication", "UnregisterApplication",
                                     "UnregisterAgent"]
    assert per.state == per.OFF and not (per.app_registered or per.agent_registered)
    assert per.ui.errors == []


def test_stop_while_a_register_fails(mod, per, capsys):
    appear(per, powered=True, pairable=True)
    per.start()
    per.stop()
    calls = per.bus_.run(fail={"RegisterAgent": "GDBus.Error:org.bluez.Error.Failed: busy",
                               "UnregisterAgent": "GDBus.Error:org.bluez.Error.DoesNotExist: no"})
    assert [m for m, _ in calls] == ["RegisterAgent", "UnregisterAgent"]
    # Stopping is what the user asked for: no error for the start that was given up
    assert per.state == per.OFF and per.ui.errors == []
    err = capsys.readouterr().err
    assert "RegisterAgent failed" in err and "UnregisterAgent failed" in err


def test_stop_while_switching_on_registers_nothing_later(mod, per):
    appear(per)
    per.start()
    per.stop()  # Powered not answered yet
    calls = per.bus_.run()
    assert [m for m, _ in calls] == ["Set", "Set"]  # Powered, then Pairable back
    assert calls[1][1] == ("org.bluez.Adapter1", "Pairable", False)
    assert per.state == per.OFF and not per.agent_registered


def test_stop_while_registering_the_advertisement(mod, per):
    appear(per, powered=True, pairable=True)
    per.start()
    for method in ("RegisterAgent", "RequestDefaultAgent", "RegisterApplication"):
        assert per.bus_.answer().method == method
    assert per.state == per.VISIBLE and per.bus_.pending[0].method == "RegisterAdvertisement"
    per.stop()  # before BlueZ answered: the phone must not stay visible
    assert [m for m, _ in per.bus_.run()] == ["RegisterAdvertisement", "UnregisterAdvertisement",
                                             "UnregisterApplication", "UnregisterAgent"]
    assert per.state == per.OFF and not per.adv_registered and not per.adv_busy
    # Visible again on the next start
    per.start()
    assert per.bus_.run()[-1][0] == "RegisterAdvertisement" and per.adv_registered


def test_stop_while_unregistering_the_advertisement(mod, per):
    start(mod, per, powered=True, pairable=True)
    method(per, mod.CHRC_IFACE, "StartNotify", GLib.Variant("()", ()),
           path=per.reports[mod.RID_KEYBOARD].path)
    assert per.bus_.pending[0].method == "UnregisterAdvertisement"
    per.stop()  # the advertisement is on its way out already: not a second time
    assert [m for m, _ in per.bus_.run()] == ["UnregisterAdvertisement", "UnregisterApplication",
                                             "UnregisterAgent"]
    assert per.state == per.OFF and not per.adv_registered


def test_failing_advertisement_after_stop_is_no_error(mod, per, capsys):
    appear(per, powered=True, pairable=True)
    per.start()
    for _ in range(3):
        per.bus_.answer()
    per.stop()
    per.bus_.run(fail={"RegisterAdvertisement": "no slot",
                       "UnregisterAdvertisement": "GDBus.Error:org.bluez.Error.DoesNotExist: no"})
    assert per.ui.errors == [] and per.state == per.OFF
    assert "RegisterAdvertisement failed: no slot" in capsys.readouterr().err


def test_bluez_gone_while_starting_then_a_new_start(mod, per):
    appear(per, powered=True, pairable=True)
    per.start()
    old = per.bus_.pending.pop(0)  # RegisterAgent of the first start, answered late
    per.on_bluez_vanished(None, None)
    appear(per, powered=True, pairable=True)
    per.start()
    assert per.bus_.answer().method == "RegisterAgent"
    old.finish(per.bus_, GLib.Variant("()", ()))  # must not drive the second start
    assert [c.method for c in per.bus_.pending] == ["RequestDefaultAgent"]
    assert [m for m, _ in per.bus_.run()] == ["RequestDefaultAgent", "RegisterApplication",
                                             "RegisterAdvertisement"]
    assert per.state == per.VISIBLE


def test_bluez_leaving_while_starting(mod, per):
    appear(per, powered=True, pairable=True)
    per.start()
    per.on_bluez_vanished(None, None)
    per.bus_.run(fail={"RegisterAgent": "GDBus.Error:org.freedesktop.DBus.Error."
                                        "ServiceUnknown: gone"})
    # The failed start of a BlueZ that is gone is no news for the user
    assert per.ui.errors == [] and per.state == per.OFF


def test_failing_unregister_when_quitting_is_logged(mod, per, capsys):
    start(mod, per, powered=True, pairable=True)
    per.bus_.sync_answers["UnregisterApplication"] = GLib.Error("Timeout was reached")
    per.stop(sync=True)
    assert [c[1] for c in per.bus_.sync] == ["UnregisterAdvertisement", "UnregisterApplication",
                                            "UnregisterAgent"]
    assert "UnregisterApplication failed: Timeout was reached" in capsys.readouterr().err
    assert per.state == per.OFF


def test_failing_unregister_advertisement_is_tried_again(mod, per):
    start(mod, per, powered=True, pairable=True)
    method(per, mod.CHRC_IFACE, "StartNotify", GLib.Variant("()", ()),
           path=per.reports[mod.RID_KEYBOARD].path)
    assert per.bus_.answer(error=GLib.Error("busy")).method == "UnregisterAdvertisement"
    assert per.adv_registered and per.ui.errors == []
    assert per.bus_.answer().method == "UnregisterAdvertisement"
    assert not per.adv_registered and per.bus_.pending == []


# ----- report pacing: the rest -----

def test_kick_with_nothing_to_send(mod, reports, glib):
    reports.kick()
    assert reports.sent == [] and reports.timer
    drain(glib, reports)
    assert reports.timer == 0 and reports.sent == []


def test_zoom_sends_the_motion_first(mod, reports, glib):
    reports.timer = 99
    reports.dx = 3.0
    reports.zoom(1)
    assert reports.queue[0] == mouse(x=3) and reports.queue[2] == mouse(wheel=1)


def test_history_backspace_with_nothing_typed(mod, history):
    changes = []
    history.changed = lambda: changes.append(1)
    history.backspace()
    assert changes == [] and history.empty()


# ----- the touchpad: the rest of the gestures -----

def test_two_fingers_move_no_pointer(mod, pad):
    pad.on_drag_begin(Gesture(), 0, 0)
    pad.on_two_begin(Gesture(), None)
    pad.glib.advance_ms(20)
    pad.on_drag_update(None, 40, 0)
    assert pad.reports.dx == 0 and pad.sent == []


def test_tap_while_the_button_is_held_lets_go(mod, pad):
    pad.hold_toggle.set_active(True)
    pad.on_drag_begin(Gesture(), 0, 0)
    pad.glib.advance_ms(50)
    pad.on_drag_end(None, 0, 0)
    assert not pad.hold_toggle.get_active()
    assert buttons(flush(pad)) == [1, 0]  # held, then let go: no extra click
    assert pad.last_tap == 0


def test_long_press_after_moving_holds_nothing(mod, pad):
    pad.on_drag_begin(Gesture(), 0, 0)
    pad.moved = 20.0  # moved away before the timer fired
    lp = pad.long_press
    pad.glib.fire(lp)
    assert not pad.holding and pad.reports.buttons == 0


def test_second_finger_ends_tap_and_drag(mod, pad):
    pad.on_drag_begin(Gesture(), 0, 0)
    pad.glib.advance_ms(50)
    pad.on_drag_end(None, 0, 0)
    pad.glib.advance_ms(100)
    pad.on_drag_begin(Gesture(), 0, 0)
    assert pad.tap_drag and pad.reports.buttons == 1
    pad.on_two_begin(Gesture(), None)
    assert not pad.tap_drag and pad.reports.buttons == 0


class NoCenter(Gesture):
    def get_bounding_box_center(self):
        return (False, 0.0, 0.0)


def test_two_fingers_without_a_center(mod, pad):
    g = NoCenter()
    pad.on_two_begin(g, None)
    assert pad.zoom_center is None
    pad.on_two_update(g, None)  # still none: nothing to compare with
    assert pad.zoom_center is None
    # The first update with a center only starts the tracking
    pad.on_two_update(Gesture(center=(50.0, 50.0)), None)
    assert pad.zoom_center == (50.0, 50.0) and pad.zoom_mode is None


def test_small_moves_decide_nothing(mod, pad):
    g = Gesture()
    pad.on_two_begin(g, None)
    g.center = (103.0, 102.0)
    pad.on_two_update(g, None)
    assert pad.zoom_mode is None and pad.sent == []
    # Moving more than a few pixels is a tap no longer
    g.center = (130.0, 102.0)
    pad.on_two_update(g, None)
    pad.on_two_end(g, None)
    sent = flush(pad)
    assert pad.zoom_mode == "scroll" and sent and all(d[0] == 0 for _r, d in sent)


def test_zoom_below_a_step_sends_nothing(mod, pad):
    g = Gesture()
    pad.on_two_begin(g, None)
    g.scale = 1.2
    pad.on_two_update(g, None)
    flush(pad)
    count = len(pad.sent)
    g.scale = 1.24  # one step is 1.12; the second would be at 1.2544
    pad.on_two_update(g, None)
    assert len(pad.sent) == count and pad.reports.queue == []


def test_two_finger_updates_with_three_fingers_down(mod, pad):
    touches(pad, mod, "TOUCH_BEGIN", {1: (100, 300), 2: (150, 300), 3: (200, 300)})
    g = Gesture()
    pad.on_two_begin(g, None)
    g.center, g.scale = (100.0, 160.0), 2.0
    pad.on_two_update(g, None)
    assert pad.zoom_mode is None and pad.sent == []


def test_touch_updates_of_unknown_fingers(mod, pad):
    touches(pad, mod, "TOUCH_BEGIN", {1: (100, 300)})
    touches(pad, mod, "TOUCH_UPDATE", {7: (500, 500)})  # began before the pad saw it
    touches(pad, mod, "TOUCH_UPDATE", {1: (120, 300)})
    assert pad.touches == {1: (120, 300)}
    touches(pad, mod, "TOUCH_CANCEL", {1: (120, 300)})
    assert pad.touches == {} and buttons(flush(pad)) == []


def test_three_fingers_held_long_click_nothing(mod, pad):
    start_ = {1: (100, 300), 2: (150, 300), 3: (200, 300)}
    touches(pad, mod, "TOUCH_BEGIN", start_)
    pad.glib.advance_ms(1000)
    touches(pad, mod, "TOUCH_END", start_)
    assert flush(pad) == [] and pad.three_start is None
