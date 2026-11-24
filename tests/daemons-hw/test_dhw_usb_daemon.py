# plinius-usb's own main() on a private bus (a dbus-daemon of the test, given to it as the system
# bus): it takes org.plinius.Usb, configures what it finds at the start (configfs, a computer
# attached, the preset), answers methods and properties over the bus, tells changes as
# PropertiesChanged, and leaves nothing offered to a computer when it is stopped. Sysfs, configfs
# and programs as in test_dhw_usb.py (dhw_usb_phone.py); logind answers as scripted there.

import time

import pytest

from dhw_usb_phone import Daemon, Phone, PrivateBus, build

gi = pytest.importorskip("gi")
from gi.repository import Gio, GLib  # noqa: E402

NAME, PATH, IFACE = "org.plinius.Usb", "/org/plinius/Usb", "org.plinius.Usb"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("usb-daemon")
    return d, build(d)


@pytest.fixture
def phone(built):
    return Phone(*built).reset()


@pytest.fixture
def bus(tmp_path):
    b = PrivateBus(tmp_path)
    yield b
    b.close()


@pytest.fixture
def client(bus):
    c = Gio.DBusConnection.new_for_address_sync(
        bus.address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
    yield c
    c.close_sync(None)


@pytest.fixture
def start(phone, bus, client):
    daemons = []

    def go(wait_for_name=True):
        d = Daemon(phone, bus.address)
        daemons.append(d)
        if wait_for_name:
            end = time.monotonic() + 20 * d.slow
            while not has_owner(client):
                assert time.monotonic() < end, d.stderr
                time.sleep(0.02)
        return d

    yield go
    for d in daemons:
        d.kill()


def has_owner(client):
    return client.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                            "NameHasOwner", GLib.Variant("(s)", (NAME,)), None, 0, 5000, None)[0]


def call(client, method, params, iface=IFACE, timeout=10000):
    return client.call_sync(NAME, PATH, iface, method, params, None, 0, timeout, None)


def get(client, prop):
    return call(client, "Get", GLib.Variant("(ss)", (IFACE, prop)), "org.freedesktop.DBus.Properties")[0]


def dbus_error(excinfo):
    e = excinfo.value
    name = Gio.DBusError.get_remote_error(e)
    return name, e.message.removeprefix(f"GDBus.Error:{name}: ")


def test_start_with_a_computer_attached(phone, start, client):
    phone.computer()
    d = start()
    d.command("state")   # everything it printed so far is in d.lines
    # Not mounted yet: configfs is mounted first
    assert f"mount none {phone.d}/cfg configfs" in d.lines
    assert "udev power_supply,typec,extcon" in d.lines
    props = call(client, "GetAll", GLib.Variant("(s)", (IFACE,)), "org.freedesktop.DBus.Properties")[0]
    assert props["Connected"] is True
    assert props["Functions"] == ["ncm"]
    assert props["AvailableFunctions"] == ["mtp", "ncm", "gateway", "webcam"]
    assert props["NetworkOnConnect"] is True
    assert props["PowerRole"] == ""
    assert props["ActivePort"] == "none"
    assert phone.gadget("UDC").read_text() == "11201000.usb"
    assert (phone.d / "run/plinius-usb").is_dir()
    assert "Type-C port: none" in d.stderr
    assert "Computer attached" in d.stderr
    xml = call(client, "Introspect", None, "org.freedesktop.DBus.Introspectable")[0]
    for member in ("SetFunctions", "ChoosePort", "SetPowerRole", "SupplyPowerOnConnect"):
        assert f"name=\"{member}\"" in xml

    # A method over the bus: the gateway on, its change told as a signal
    changes = []
    client.signal_subscribe(NAME, "org.freedesktop.DBus.Properties", "PropertiesChanged", PATH, None,
                            Gio.DBusSignalFlags.NONE,
                            lambda *a: changes.append(a[5].unpack()[1]))
    call(client, "SetFunctions", GLib.Variant("(as)", (["gateway"],)))
    assert get(client, "Functions") == ["ncm", "gateway"]
    end = time.monotonic() + 10
    while {"Functions": ["ncm", "gateway"]} not in changes and time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
    assert {"Functions": ["ncm", "gateway"]} in changes

    with pytest.raises(GLib.Error) as e:
        call(client, "SetFunctions", GLib.Variant("(as)", (["printer"],)))
    assert dbus_error(e) == ("org.freedesktop.DBus.Error.InvalidArgs", "Unknown function printer")

    # A property set over the bus is saved
    call(client, "Set", GLib.Variant("(ssv)", (IFACE, "NetworkOnConnect", GLib.Variant("b", False))),
         "org.freedesktop.DBus.Properties")
    assert "network-on-connect=false" in phone.get("var/settings")

    # Stopped as systemd does: nothing stays offered to the computer
    assert d.command("quit") == []
    assert d.finish() == 0
    assert phone.gadget("UDC").read_text() == "\n"
    assert not phone.gadget("functions/ncm.usb0").exists()
    assert phone.bin.calls("networkctl") == [["reload"], ["reload"]]


def test_start_without_a_computer(phone, start, client):
    phone.gadget("functions").mkdir(parents=True)
    phone.put("var/settings", "[usb]\nnetwork-on-connect=false\nwebcam-camera=hal_camera_0\n")
    d = start()
    d.command("state")
    # configfs is there already
    assert not [line for line in d.lines if line.startswith("mount ")]
    assert get(client, "Connected") is False
    assert get(client, "WebcamCamera") == "hal_camera_0"
    assert get(client, "NetworkOnConnect") is False
    assert "Computer not attached" in d.stderr
    # Plugged in later: the settings say no network
    phone.computer()
    d.command("uevent power_supply USB")
    end = time.monotonic() + 10 * d.slow
    while get(client, "Connected") is False:
        assert time.monotonic() < end
        time.sleep(0.02)
    assert get(client, "Functions") == []
    d.proc.send_signal(15)
    assert d.finish() == 0


def test_power_role_over_the_bus(phone, start, client):
    phone.typec()
    d = start()
    assert get(client, "PowerRole") == "sink"
    # The answer comes from the swap's thread, once the partner agreed
    call(client, "SetPowerRole", GLib.Variant("(s)", ("source",)))
    assert get(client, "PowerRole") == "source"
    d.command("swap refuse")
    with pytest.raises(GLib.Error) as e:
        call(client, "SetPowerRole", GLib.Variant("(s)", ("sink",)))
    assert dbus_error(e) == ("org.freedesktop.DBus.Error.Failed", "The connected device does not supply power")
    with pytest.raises(GLib.Error) as e:
        call(client, "Set", GLib.Variant("(ssv)", (IFACE, "Connected", GLib.Variant("b", True))),
             "org.freedesktop.DBus.Properties")
    # Read-only as introspected: GDBus refuses it before plinius-usb sees it
    assert dbus_error(e) == ("org.freedesktop.DBus.Error.InvalidArgs", "Property “Connected” is not writable")
    d.command("quit")
    assert d.finish() == 0


def test_a_second_instance_ends(phone, bus, client, start):
    # Someone has the name already: plinius-usb gives up and leaves nothing behind
    client.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                     GLib.Variant("(su)", (NAME, 4)), None, 0, 5000, None)
    phone.computer()
    d = start(wait_for_name=False)
    assert d.finish() == 0
    assert "Lost or could not own org.plinius.Usb" in d.stderr
    assert phone.gadget("UDC").read_text() == "\n"
