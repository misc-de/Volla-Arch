# plinius-pbap-ask (pkgbuilds/bluez-obex-plinius): who may read the contacts over Bluetooth.
# The address check, the first start (paired devices keep their access), and what an answer
# to the notification writes into the list obexd reads.
#
# No D-Bus: bluez_devices and the session bus are replaced by fakes, the list lives in tmp_path.

import os
import stat

import pytest

from conftest import PKG, load_script

SCRIPT = PKG / "bluez-obex-plinius" / "plinius-pbap-ask"
CAR = "AA:BB:CC:DD:EE:FF"


@pytest.fixture(scope="module")
def mod():
    pytest.importorskip("gi")
    return load_script(SCRIPT)


@pytest.fixture
def store(mod, tmp_path, monkeypatch):
    path = tmp_path / "data" / "plinius" / "bluetooth-contacts"
    monkeypatch.setattr(mod, "store_path", lambda: str(path))
    return path


def allowed(mod, address):
    kf = mod.load()
    try:
        return kf.get_boolean("devices", address)
    except mod.GLib.Error:
        return None


# --- Arguments ---------------------------------------------------------------------------------

@pytest.mark.parametrize("arg", ["aa:bb:cc:dd:ee:ff", CAR])
def test_main_accepts_addresses(mod, monkeypatch, arg):
    asked = []
    monkeypatch.setattr(mod, "ask", asked.append)
    mod.main(["plinius-pbap-ask", arg])
    assert asked == [CAR]


@pytest.mark.parametrize("arg", ["AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:FF:00", "AA-BB-CC-DD-EE-FF",
                                 "GG:BB:CC:DD:EE:FF", "", "--help", "[devices]", "AA:BB:CC:DD:EE:FF=true"])
def test_main_refuses_other_arguments(mod, monkeypatch, arg):
    monkeypatch.setattr(mod, "ask", lambda a: pytest.fail("asked"))
    assert mod.main(["plinius-pbap-ask", arg]) == 2


def test_main_usage(mod):
    assert mod.main(["plinius-pbap-ask"]) == 2
    assert mod.main(["plinius-pbap-ask", CAR, "x"]) == 2


@pytest.mark.parametrize("env, lang", [
    ({}, "en"), ({"LANG": "de_DE.UTF-8"}, "de"), ({"LANGUAGE": "fr:de", "LANG": "de_DE.UTF-8"}, "fr"),
    ({"LANGUAGE": "eo:pt_BR", "LANG": "C"}, "pt"), ({"LC_ALL": "C", "LANG": "de_DE.UTF-8"}, "en"),
    ({"LANG": "ja_JP.UTF-8"}, "en"), ({"LC_MESSAGES": "ru_RU.UTF-8"}, "ru"),
])
def test_texts_follow_the_language(mod, monkeypatch, env, lang):
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    assert mod.texts() == mod.TEXTS[lang]


def test_all_translations_complete(mod):
    for lang, (summary, body, allow, deny) in mod.TEXTS.items():
        assert "{name}" in summary and "{name}" in body, lang
        assert allow and deny and allow != deny


# --- First start -------------------------------------------------------------------------------

def test_init_allows_devices_paired_already(mod, store, monkeypatch):
    monkeypatch.setattr(mod, "bluez_devices", lambda: [(CAR, "Car", True), ("11:22:33:44:55:66", "Shop", False)])
    assert mod.init() == 0
    assert allowed(mod, CAR) is True
    assert allowed(mod, "11:22:33:44:55:66") is None  # not paired: asked when it tries
    # Only the user can read the list
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.parent.stat().st_mode) == 0o700


def test_init_runs_only_once(mod, store, monkeypatch):
    store.parent.mkdir(parents=True)
    store.write_text("[devices]\nAA:BB:CC:DD:EE:FF=false\n")
    monkeypatch.setattr(mod, "bluez_devices", lambda: pytest.fail("must not look"))
    assert mod.init() == 0
    assert allowed(mod, CAR) is False


def test_init_waits_for_bluetoothd_and_gives_up(mod, store, monkeypatch):
    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)

    def unavailable():
        raise mod.GLib.Error("org.freedesktop.DBus.Error.ServiceUnknown")
    monkeypatch.setattr(mod, "bluez_devices", unavailable)
    assert mod.init() == 0
    assert len(sleeps) == 10
    # The list exists (empty), so a later start does not allow devices paired meanwhile
    assert store.exists() and allowed(mod, CAR) is None


# --- Asking ------------------------------------------------------------------------------------

class FakeBus:
    def __init__(self, mod, answer):
        self.mod, self.answer, self.calls, self.handler = mod, answer, [], None

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, callback):
        self.handler = callback

    def call_sync(self, name, path, iface, method, params, reply_type, flags, timeout):
        self.calls.append((method, params.unpack() if params is not None else None))
        if method == "Notify":
            return self.mod.GLib.Variant("(u)", (42,))
        return None


@pytest.fixture
def notification(mod, store, monkeypatch):
    """The session bus with a notification server that answers `answer` to notification 42."""
    setup = {"answer": None, "bus": None, "devices": [(CAR, "My Car", True)]}
    monkeypatch.setattr(mod, "bluez_devices", lambda: setup["devices"])

    class Gio:
        BusType = mod.Gio.BusType
        DBusSignalFlags = mod.Gio.DBusSignalFlags
        DBusCallFlags = mod.Gio.DBusCallFlags

        @staticmethod
        def bus_get_sync(bus_type):
            assert bus_type == mod.Gio.BusType.SESSION
            setup["bus"] = FakeBus(mod, setup["answer"])
            return setup["bus"]

    class MainLoop:
        def run(self):
            bus, answer = setup["bus"], setup["answer"]
            V = mod.GLib.Variant
            # A signal for someone else's notification first: ignored
            bus.handler(None, None, None, None, "ActionInvoked", V("(us)", (7, "allow")))
            if answer == "closed":
                bus.handler(None, None, None, None, "NotificationClosed", V("(uu)", (42, 2)))
            elif answer is not None:
                bus.handler(None, None, None, None, "ActionInvoked", V("(us)", (42, answer)))

        def quit(self):
            pass

    real_glib = mod.GLib
    proxy = type("GLibProxy", (), {
        "__getattr__": lambda self, name: getattr(real_glib, name),
        "MainLoop": MainLoop,
        "timeout_add_seconds": staticmethod(lambda seconds, function: setup.setdefault("timeout", seconds)),
    })()
    monkeypatch.setattr(mod, "Gio", Gio)
    monkeypatch.setattr(mod, "GLib", proxy)
    return setup


@pytest.mark.parametrize("answer, expected", [("allow", True), ("deny", False)])
def test_answer_is_kept(mod, notification, answer, expected):
    notification["answer"] = answer
    assert mod.ask(CAR) == 0
    assert allowed(mod, CAR) is expected
    method, (app, _id, _icon, summary, body, actions, hints, _t) = notification["bus"].calls[0]
    assert method == "Notify"
    assert "My Car" in summary and "My Car" in body
    assert actions[0::2] == ["allow", "deny"]
    assert notification["timeout"] == mod.ANSWER_TIMEOUT_S


@pytest.mark.parametrize("answer", ["closed", "default"])
def test_closed_without_answer_decides_nothing(mod, notification, store, answer):
    notification["answer"] = answer
    assert mod.ask(CAR) == 0
    assert allowed(mod, CAR) is None


def test_timeout_withdraws_the_question(mod, notification, store):
    notification["answer"] = None  # nobody answers until the timeout
    assert mod.ask(CAR) == 0
    assert allowed(mod, CAR) is None
    assert notification["bus"].calls[-1] == ("CloseNotification", (42,))


def test_decided_device_is_not_asked_again(mod, notification, store):
    store.parent.mkdir(parents=True)
    store.write_text("[devices]\nAA:BB:CC:DD:EE:FF=false\n")
    assert mod.ask(CAR) == 0
    assert notification["bus"] is None


def test_unknown_device_is_named_by_its_address(mod, notification):
    notification["devices"] = []
    notification["answer"] = "deny"
    mod.ask(CAR)
    assert CAR in notification["bus"].calls[0][1][3]


def test_answer_keeps_other_entries_and_comments(mod, notification, store):
    store.parent.mkdir(parents=True)
    store.write_text("# Who may read the contacts\n[devices]\n11:22:33:44:55:66=true\n")
    notification["answer"] = "allow"
    mod.ask(CAR)
    text = store.read_text()
    assert "# Who may read the contacts" in text
    assert allowed(mod, "11:22:33:44:55:66") is True and allowed(mod, CAR) is True
    assert not [p for p in os.listdir(store.parent) if p != store.name]  # no temporary file left


def test_address_with_a_trailing_newline_is_refused(mod, notification, store):
    store.parent.mkdir(parents=True)
    store.write_text("[devices]\n11:22:33:44:55:66=false\n")
    notification["answer"] = "allow"
    code = mod.main(["plinius-pbap-ask", CAR + "\n"])
    # The list must stay a valid key file (GLib.Error here today: "not a key-value pair")
    mod.GLib.KeyFile().load_from_file(str(store), mod.GLib.KeyFileFlags.NONE)
    assert code == 2


# --- bluetoothd's devices, the store's place, the program ---------------------------------------

def test_store_path_is_in_the_users_data_dir(mod):
    assert mod.store_path() == os.path.join(mod.GLib.get_user_data_dir(), "plinius",
                                            "bluetooth-contacts")


class BluezBus:
    def __init__(self, mod, objects):
        self.mod, self.objects, self.calls = mod, objects, []

    def call_sync(self, name, path, iface, method, params, reply_type, flags, timeout):
        self.calls.append((name, path, iface, method, params, reply_type.dup_string(), timeout))
        V = self.mod.GLib.Variant
        return V("(a{oa{sa{sv}}})", (self.objects,))


def test_bluez_devices_reads_bluetoothds_objects(mod, monkeypatch):
    V = mod.GLib.Variant
    objects = {
        "/org/bluez/hci0": {"org.bluez.Adapter1": {"Address": V("s", "00:11:22:33:44:55")}},
        "/org/bluez/hci0/dev_AA": {"org.bluez.Device1": {
            "Address": V("s", "aa:bb:cc:dd:ee:ff"), "Alias": V("s", "My Car"),
            "Paired": V("b", True)}},
        "/org/bluez/hci0/dev_11": {"org.bluez.Device1": {
            "Address": V("s", "11:22:33:44:55:66")}},
        "/org/bluez/hci0/dev_x": {"org.bluez.Device1": {"Alias": V("s", "no address")}},
    }
    bus = BluezBus(mod, objects)
    kinds = []
    monkeypatch.setattr(mod.Gio, "bus_get_sync", lambda kind: kinds.append(kind) or bus)
    assert sorted(mod.bluez_devices()) == [("11:22:33:44:55:66", "11:22:33:44:55:66", False),
                                           (CAR, "My Car", True)]
    assert kinds == [mod.Gio.BusType.SYSTEM]
    (name, path, iface, method, params, reply, timeout), = bus.calls
    assert (name, path, iface, method, params) == (
        "org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects", None)
    assert reply == "(a{oa{sa{sv}}})" and timeout == 5000


def test_ask_names_the_device_among_others(mod, notification):
    notification["devices"] = [("11:22:33:44:55:66", "Headphones", True), (CAR, "My Car", True),
                               ("22:33:44:55:66:77", "Watch", False)]
    notification["answer"] = "deny"
    mod.ask(CAR)
    summary = notification["bus"].calls[0][1][3]
    assert "My Car" in summary and "Headphones" not in summary and "Watch" not in summary


def test_ask_without_bluetoothd_names_the_address(mod, notification, monkeypatch):
    def unavailable():
        raise mod.GLib.Error("org.freedesktop.DBus.Error.ServiceUnknown")
    monkeypatch.setattr(mod, "bluez_devices", unavailable)
    notification["answer"] = "allow"
    assert mod.ask(CAR) == 0
    assert CAR in notification["bus"].calls[0][1][3] and allowed(mod, CAR) is True


def test_timeout_withdraw_failing_is_harmless(mod, notification, store, monkeypatch):
    real_call = FakeBus.call_sync

    def call_sync(self, name, path, iface, method, params, reply_type, flags, timeout):
        result = real_call(self, name, path, iface, method, params, reply_type, flags, timeout)
        if method == "CloseNotification":
            raise mod.GLib.Error("notification server gone")
        return result
    monkeypatch.setattr(FakeBus, "call_sync", call_sync)
    notification["answer"] = None
    assert mod.ask(CAR) == 0
    assert notification["bus"].calls[-1] == ("CloseNotification", (42,))
    assert allowed(mod, CAR) is None


def test_main_init(mod, monkeypatch):
    monkeypatch.setattr(mod, "init", lambda: 0)
    monkeypatch.setattr(mod, "ask", lambda a: pytest.fail("--init asks nobody"))
    assert mod.main(["plinius-pbap-ask", "--init"]) == 0


def test_as_a_program_refuses_a_non_address(monkeypatch, capsys):
    import runpy
    import sys
    monkeypatch.setattr(sys, "argv", ["plinius-pbap-ask", "not-an-address"])
    with pytest.raises(SystemExit) as exit_:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exit_.value.code == 2
    assert "not-an-address: not a Bluetooth address" in capsys.readouterr().err
