# A fake NetworkManager on the test's private system bus (tests/gcc), enough for libnm's NMClient:
# the manager, the settings with their connections, devices (Wi-Fi, Ethernet, any other type) and
# the active connections that activating makes, all exported under an ObjectManager at
# /org/freedesktop as NetworkManager does.
#
# In a test:  nm = FakeNM(gcc_world); nm.device("wlan0"); nm.connection({...}); nm.start()
# (before gcc_world.run). The calls go into the world's call log (gcc_world.calls(NM_NAME, member)),
# in gcc_fakebus.py's format, with name "org.freedesktop.NetworkManager".
#
# As a program: python3 -I fake_nm.py SPEC.json (DBUS_SYSTEM_BUS_ADDRESS names the bus); prints
# READY when the name is owned. SPEC: {"log": FILE, "devices": [...], "connections": [...],
# "replies": {method: {"error": name, "message": text}}, "manager": {prop: GVariant text}}.
# Methods without a reply in "replies" do what NetworkManager does: AddConnection and
# AddAndActivateConnection add a connection object, the activations add an active connection.

import json
import os
import subprocess
import sys
from pathlib import Path

NM_NAME = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
SETTINGS_PATH = NM_PATH + "/Settings"
NM_IFACE = "org.freedesktop.NetworkManager"
SETTINGS_IFACE = NM_IFACE + ".Settings"
CONNECTION_IFACE = NM_IFACE + ".Settings.Connection"
DEVICE_IFACE = NM_IFACE + ".Device"
WIRELESS_IFACE = NM_IFACE + ".Device.Wireless"
WIRED_IFACE = NM_IFACE + ".Device.Wired"
ACTIVE_IFACE = NM_IFACE + ".Connection.Active"

# NMDeviceType
DEVICE_TYPES = {"ethernet": 1, "wifi": 2, "modem": 8, "bridge": 13, "wireguard": 29}

INTERFACES = {
    NM_IFACE: """
  <method name="GetDevices"><arg name="devices" type="ao" direction="out"/></method>
  <method name="GetAllDevices"><arg name="devices" type="ao" direction="out"/></method>
  <method name="ActivateConnection"><arg name="connection" type="o" direction="in"/>
   <arg name="device" type="o" direction="in"/><arg name="specific_object" type="o" direction="in"/>
   <arg name="active_connection" type="o" direction="out"/></method>
  <method name="AddAndActivateConnection"><arg name="connection" type="a{sa{sv}}" direction="in"/>
   <arg name="device" type="o" direction="in"/><arg name="specific_object" type="o" direction="in"/>
   <arg name="path" type="o" direction="out"/><arg name="active_connection" type="o" direction="out"/></method>
  <method name="AddAndActivateConnection2"><arg name="connection" type="a{sa{sv}}" direction="in"/>
   <arg name="device" type="o" direction="in"/><arg name="specific_object" type="o" direction="in"/>
   <arg name="options" type="a{sv}" direction="in"/>
   <arg name="path" type="o" direction="out"/><arg name="active_connection" type="o" direction="out"/>
   <arg name="result" type="a{sv}" direction="out"/></method>
  <method name="DeactivateConnection"><arg name="active_connection" type="o" direction="in"/></method>
  <method name="GetPermissions"><arg name="permissions" type="a{ss}" direction="out"/></method>
  <method name="Enable"><arg name="enable" type="b" direction="in"/></method>
  <signal name="CheckPermissions"/>
  <signal name="StateChanged"><arg name="state" type="u"/></signal>
  <signal name="DeviceAdded"><arg name="device_path" type="o"/></signal>
  <signal name="DeviceRemoved"><arg name="device_path" type="o"/></signal>
  <property name="Devices" type="ao" access="read"/>
  <property name="AllDevices" type="ao" access="read"/>
  <property name="Checkpoints" type="ao" access="read"/>
  <property name="NetworkingEnabled" type="b" access="read"/>
  <property name="WirelessEnabled" type="b" access="readwrite"/>
  <property name="WirelessHardwareEnabled" type="b" access="read"/>
  <property name="WwanEnabled" type="b" access="readwrite"/>
  <property name="WwanHardwareEnabled" type="b" access="read"/>
  <property name="ActiveConnections" type="ao" access="read"/>
  <property name="PrimaryConnection" type="o" access="read"/>
  <property name="PrimaryConnectionType" type="s" access="read"/>
  <property name="Metered" type="u" access="read"/>
  <property name="ActivatingConnection" type="o" access="read"/>
  <property name="Startup" type="b" access="read"/>
  <property name="Version" type="s" access="read"/>
  <property name="Capabilities" type="au" access="read"/>
  <property name="State" type="u" access="read"/>
  <property name="Connectivity" type="u" access="read"/>
  <property name="ConnectivityCheckAvailable" type="b" access="read"/>
  <property name="ConnectivityCheckEnabled" type="b" access="readwrite"/>""",
    SETTINGS_IFACE: """
  <method name="ListConnections"><arg name="connections" type="ao" direction="out"/></method>
  <method name="GetConnectionByUuid"><arg name="uuid" type="s" direction="in"/>
   <arg name="connection" type="o" direction="out"/></method>
  <method name="AddConnection"><arg name="connection" type="a{sa{sv}}" direction="in"/>
   <arg name="path" type="o" direction="out"/></method>
  <method name="AddConnectionUnsaved"><arg name="connection" type="a{sa{sv}}" direction="in"/>
   <arg name="path" type="o" direction="out"/></method>
  <method name="AddConnection2"><arg name="settings" type="a{sa{sv}}" direction="in"/>
   <arg name="flags" type="u" direction="in"/><arg name="args" type="a{sv}" direction="in"/>
   <arg name="path" type="o" direction="out"/><arg name="result" type="a{sv}" direction="out"/></method>
  <method name="ReloadConnections"><arg name="status" type="b" direction="out"/></method>
  <signal name="NewConnection"><arg name="connection" type="o"/></signal>
  <signal name="ConnectionRemoved"><arg name="connection" type="o"/></signal>
  <property name="Connections" type="ao" access="read"/>
  <property name="Hostname" type="s" access="read"/>
  <property name="CanModify" type="b" access="read"/>""",
    CONNECTION_IFACE: """
  <method name="Update"><arg name="properties" type="a{sa{sv}}" direction="in"/></method>
  <method name="UpdateUnsaved"><arg name="properties" type="a{sa{sv}}" direction="in"/></method>
  <method name="Delete"/>
  <method name="GetSettings"><arg name="settings" type="a{sa{sv}}" direction="out"/></method>
  <method name="GetSecrets"><arg name="setting_name" type="s" direction="in"/>
   <arg name="secrets" type="a{sa{sv}}" direction="out"/></method>
  <method name="ClearSecrets"/>
  <method name="Save"/>
  <method name="Update2"><arg name="settings" type="a{sa{sv}}" direction="in"/>
   <arg name="flags" type="u" direction="in"/><arg name="args" type="a{sv}" direction="in"/>
   <arg name="result" type="a{sv}" direction="out"/></method>
  <signal name="Updated"/>
  <signal name="Removed"/>
  <property name="Unsaved" type="b" access="read"/>
  <property name="Flags" type="u" access="read"/>
  <property name="Filename" type="s" access="read"/>""",
    DEVICE_IFACE: """
  <method name="Disconnect"/>
  <method name="Delete"/>
  <signal name="StateChanged"><arg name="new_state" type="u"/><arg name="old_state" type="u"/>
   <arg name="reason" type="u"/></signal>
  <property name="Udi" type="s" access="read"/>
  <property name="Path" type="s" access="read"/>
  <property name="Interface" type="s" access="read"/>
  <property name="IpInterface" type="s" access="read"/>
  <property name="Driver" type="s" access="read"/>
  <property name="DriverVersion" type="s" access="read"/>
  <property name="FirmwareVersion" type="s" access="read"/>
  <property name="Capabilities" type="u" access="read"/>
  <property name="State" type="u" access="read"/>
  <property name="StateReason" type="(uu)" access="read"/>
  <property name="ActiveConnection" type="o" access="read"/>
  <property name="Ip4Config" type="o" access="read"/>
  <property name="Dhcp4Config" type="o" access="read"/>
  <property name="Ip6Config" type="o" access="read"/>
  <property name="Dhcp6Config" type="o" access="read"/>
  <property name="Managed" type="b" access="readwrite"/>
  <property name="Autoconnect" type="b" access="readwrite"/>
  <property name="FirmwareMissing" type="b" access="read"/>
  <property name="NmPluginMissing" type="b" access="read"/>
  <property name="DeviceType" type="u" access="read"/>
  <property name="AvailableConnections" type="ao" access="read"/>
  <property name="PhysicalPortId" type="s" access="read"/>
  <property name="Mtu" type="u" access="read"/>
  <property name="Metered" type="u" access="read"/>
  <property name="Real" type="b" access="read"/>
  <property name="InterfaceFlags" type="u" access="read"/>
  <property name="HwAddress" type="s" access="read"/>""",
    WIRELESS_IFACE: """
  <method name="GetAccessPoints"><arg name="access_points" type="ao" direction="out"/></method>
  <method name="GetAllAccessPoints"><arg name="access_points" type="ao" direction="out"/></method>
  <method name="RequestScan"><arg name="options" type="a{sv}" direction="in"/></method>
  <property name="HwAddress" type="s" access="read"/>
  <property name="PermHwAddress" type="s" access="read"/>
  <property name="Mode" type="u" access="read"/>
  <property name="Bitrate" type="u" access="read"/>
  <property name="AccessPoints" type="ao" access="read"/>
  <property name="ActiveAccessPoint" type="o" access="read"/>
  <property name="WirelessCapabilities" type="u" access="read"/>
  <property name="LastScan" type="x" access="read"/>""",
    WIRED_IFACE: """
  <property name="HwAddress" type="s" access="read"/>
  <property name="PermHwAddress" type="s" access="read"/>
  <property name="Speed" type="u" access="read"/>
  <property name="S390Subchannels" type="as" access="read"/>
  <property name="Carrier" type="b" access="read"/>""",
    ACTIVE_IFACE: """
  <signal name="StateChanged"><arg name="state" type="u"/><arg name="reason" type="u"/></signal>
  <property name="Connection" type="o" access="read"/>
  <property name="SpecificObject" type="o" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Uuid" type="s" access="read"/>
  <property name="Type" type="s" access="read"/>
  <property name="Devices" type="ao" access="read"/>
  <property name="State" type="u" access="read"/>
  <property name="StateFlags" type="u" access="read"/>
  <property name="Default" type="b" access="read"/>
  <property name="Ip4Config" type="o" access="read"/>
  <property name="Dhcp4Config" type="o" access="read"/>
  <property name="Default6" type="b" access="read"/>
  <property name="Ip6Config" type="o" access="read"/>
  <property name="Dhcp6Config" type="o" access="read"/>
  <property name="Vpn" type="b" access="read"/>
  <property name="Controller" type="o" access="read"/>
  <property name="Master" type="o" access="read"/>""",
}

OBJECT_MANAGER_XML = """<node><interface name="org.freedesktop.DBus.ObjectManager">
 <method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" name="objects" direction="out"/></method>
 <signal name="InterfacesAdded"><arg type="o" name="object_path"/>
  <arg type="a{sa{sv}}" name="interfaces_and_properties"/></signal>
 <signal name="InterfacesRemoved"><arg type="o" name="object_path"/><arg type="as" name="interfaces"/></signal>
</interface></node>"""


# ----- The test's side -----

class FakeNM:
    """The fake NetworkManager of one test's world (gcc_harness.World)."""

    def __init__(self, world):
        self.world = world
        self.devices = []
        self.connections = []
        self.replies = {}
        self.manager = {}
        self.proc = None

    def device(self, iface, type="wifi", managed=True, state=30, props=None):
        """A device; returns its object path. state: NMDeviceState (30 disconnected, 100
        activated). props: {interface: {property: GVariant text}} on top."""
        n = len(self.devices) + 1
        self.devices.append({"iface": iface, "type": type, "managed": managed, "state": state,
                             "props": props or {}})
        return f"{NM_PATH}/Devices/{n}"

    def connection(self, settings):
        """A saved connection: settings as GVariant text of a{sa{sv}}; returns its object path."""
        self.connections.append(settings)
        return f"{SETTINGS_PATH}/{len(self.connections)}"

    def reply(self, method, error, message=""):
        """method answers with a D-Bus error."""
        self.replies[method] = {"error": error, "message": message}

    def start(self):
        if self.proc:
            return
        self.world.start()
        spec = self.world.tmp / "fake-nm.json"
        spec.write_text(json.dumps({"log": str(self.world.call_log), "devices": self.devices,
                                    "connections": self.connections, "replies": self.replies,
                                    "manager": self.manager}))
        env = dict(os.environ, DBUS_SYSTEM_BUS_ADDRESS=self.world.buses["system"])
        err = self.world.tmp / "fake-nm.err"
        with open(err, "w") as stderr:
            self.proc = subprocess.Popen(["python3", "-I", str(Path(__file__).resolve()), str(spec)],
                                         env=env, stdout=subprocess.PIPE, stderr=stderr, text=True)
        # World.close() ends it with the buses
        self.world.procs.insert(0, self.proc)
        line = self.proc.stdout.readline()
        if line.strip() != "READY":
            raise AssertionError(f"fake NetworkManager did not start: {line}{err.read_text()}")


# ----- The service -----

def serve(spec):
    from gi.repository import Gio, GLib

    log_fd = os.open(spec["log"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    replies = spec.get("replies", {})
    conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    infos = {name: Gio.DBusNodeInfo.new_for_xml(f'<node><interface name="{name}">{xml}</interface></node>')
             .interfaces[0] for name, xml in INTERFACES.items()}
    om_info = Gio.DBusNodeInfo.new_for_xml(OBJECT_MANAGER_XML).interfaces[0]
    # path -> {interface: {property: GVariant}}
    objects = {}
    counters = {"conn": 0, "active": 0}

    def jsonable(v):
        if isinstance(v, (list, tuple)):
            return [jsonable(x) for x in v]
        if isinstance(v, dict):
            return {str(k): jsonable(x) for k, x in v.items()}
        if isinstance(v, bytes):
            return list(v)
        return v

    def log(path, iface, member, args):
        record = {"bus": "system", "name": NM_NAME, "path": path, "interface": iface, "member": member,
                  "args": jsonable(args)}
        os.write(log_fd, (json.dumps(record) + "\n").encode())

    def zero(sig):
        if sig.startswith("a{"):
            return GLib.Variant(sig, {})
        if sig.startswith("a"):
            return GLib.Variant(sig, [])
        if sig == "o":
            return GLib.Variant(sig, "/")
        if sig == "s":
            return GLib.Variant(sig, "")
        if sig == "b":
            return GLib.Variant(sig, False)
        if sig == "(uu)":
            return GLib.Variant(sig, (0, 0))
        return GLib.Variant(sig, 0)

    def props_of(iface, values):
        out = {}
        for p in infos[iface].properties:
            v = values.get(p.name)
            if v is None:
                out[p.name] = zero(p.signature)
            elif isinstance(v, GLib.Variant):
                out[p.name] = v
            else:
                out[p.name] = GLib.Variant.parse(GLib.VariantType.new(p.signature), v, None, None)
        return out

    def emit(path, iface, signal, value):
        conn.emit_signal(None, path, iface, signal, value)

    def changed(path, iface, prop, value):
        objects[path][iface][prop] = value
        emit(path, "org.freedesktop.DBus.Properties", "PropertiesChanged",
             GLib.Variant("(sa{sv}as)", (iface, {prop: value}, [])))

    def on_get(c, sender, path, iface, prop):
        return objects.get(path, {}).get(iface, {}).get(prop)

    def on_set(c, sender, path, iface, prop, value):
        log(path, "org.freedesktop.DBus.Properties", "Set", [iface, prop, value.unpack()])
        changed(path, iface, prop, value)
        return True

    def add_object(path, ifaces):
        """ifaces: {interface: {property: value}}; exported, announced with InterfacesAdded."""
        objects[path] = {i: props_of(i, v) for i, v in ifaces.items()}
        for i in ifaces:
            conn.register_object(path, infos[i], on_call, on_get, on_set)
        emit("/org/freedesktop", "org.freedesktop.DBus.ObjectManager", "InterfacesAdded",
             GLib.Variant("(oa{sa{sv}})", (path, objects[path])))

    settings_store = {}

    def add_connection(settings):
        counters["conn"] += 1
        path = f"{SETTINGS_PATH}/{counters['conn']}"
        settings_store[path] = settings
        add_object(path, {CONNECTION_IFACE: {}})
        all_ = objects[SETTINGS_PATH][SETTINGS_IFACE]["Connections"].unpack() + [path]
        changed(SETTINGS_PATH, SETTINGS_IFACE, "Connections", GLib.Variant("ao", all_))
        emit(SETTINGS_PATH, SETTINGS_IFACE, "NewConnection", GLib.Variant("(o)", (path,)))
        return path

    def connection_setting(path, key, default=""):
        s = settings_store.get(path)
        if s is None:
            return default
        v = s.lookup_value("connection", None)
        x = v.lookup_value(key, None) if v else None
        return x.unpack() if x else default

    def add_active(connection_path, device_path, specific):
        counters["active"] += 1
        path = f"{NM_PATH}/ActiveConnection/{counters['active']}"
        add_object(path, {ACTIVE_IFACE: {
            "Connection": GLib.Variant("o", connection_path),
            "SpecificObject": GLib.Variant("o", specific or "/"),
            "Id": GLib.Variant("s", connection_setting(connection_path, "id")),
            "Uuid": GLib.Variant("s", connection_setting(connection_path, "uuid")),
            "Type": GLib.Variant("s", connection_setting(connection_path, "type")),
            "Devices": GLib.Variant("ao", [device_path] if device_path != "/" else []),
            # NM_ACTIVE_CONNECTION_STATE_ACTIVATING
            "State": GLib.Variant("u", 1)}})
        actives = objects[NM_PATH][NM_IFACE]["ActiveConnections"].unpack() + [path]
        changed(NM_PATH, NM_IFACE, "ActiveConnections", GLib.Variant("ao", actives))
        return path

    def on_call(c, sender, path, iface, method, params, invocation):
        args = params.unpack()
        log(path, iface, method, args)
        if method in replies:
            r = replies[method]
            invocation.return_dbus_error(r["error"], r.get("message", ""))
            return
        info = infos[iface].lookup_method(method)
        out_sig = "(" + "".join(a.signature for a in info.out_args) + ")"
        if iface == NM_IFACE and method in ("GetDevices", "GetAllDevices"):
            value = GLib.Variant(out_sig, (objects[NM_PATH][NM_IFACE]["Devices"].unpack(),))
        elif iface == NM_IFACE and method == "ActivateConnection":
            value = GLib.Variant(out_sig, (add_active(args[0], args[1], args[2]),))
        elif iface == NM_IFACE and method in ("AddAndActivateConnection", "AddAndActivateConnection2"):
            cpath = add_connection(params.get_child_value(0))
            apath = add_active(cpath, args[1], args[2])
            value = GLib.Variant(out_sig, (cpath, apath) if method == "AddAndActivateConnection"
                                 else (cpath, apath, {}))
        elif iface == SETTINGS_IFACE and method in ("AddConnection", "AddConnectionUnsaved", "AddConnection2"):
            cpath = add_connection(params.get_child_value(0))
            value = GLib.Variant(out_sig, (cpath,) if method != "AddConnection2" else (cpath, {}))
        elif iface == SETTINGS_IFACE and method == "ListConnections":
            value = GLib.Variant(out_sig, (objects[SETTINGS_PATH][SETTINGS_IFACE]["Connections"].unpack(),))
        elif iface == CONNECTION_IFACE and method == "GetSettings":
            value = GLib.Variant.new_tuple(settings_store[path])
        elif iface == CONNECTION_IFACE and method == "GetSecrets":
            value = GLib.Variant(out_sig, ({},))
        elif iface == NM_IFACE and method == "GetPermissions":
            value = GLib.Variant(out_sig, ({},))
        else:
            value = GLib.Variant(out_sig, tuple(zero(a.signature).unpack() for a in info.out_args))
        invocation.return_value(value)

    def on_om_call(c, sender, path, iface, method, params, invocation):
        log(path, iface, method, [])
        invocation.return_value(GLib.Variant("(a{oa{sa{sv}}})", (objects,)))

    conn.register_object("/org/freedesktop", om_info, on_om_call, None, None)

    # Before the name is owned: nothing is announced yet, GetManagedObjects tells it all
    def silent_add(path, ifaces):
        objects[path] = {i: props_of(i, v) for i, v in ifaces.items()}
        for i in ifaces:
            conn.register_object(path, infos[i], on_call, on_get, on_set)

    device_paths = []
    for n, d in enumerate(spec.get("devices", []), 1):
        path = f"{NM_PATH}/Devices/{n}"
        device_paths.append(path)
        kind = DEVICE_TYPES.get(d["type"], 0)
        ifaces = {DEVICE_IFACE: {"Interface": GLib.Variant("s", d["iface"]),
                                 "Udi": GLib.Variant("s", f"/sys/devices/virtual/net/{d['iface']}"),
                                 "DeviceType": GLib.Variant("u", kind),
                                 "Managed": GLib.Variant("b", d["managed"]),
                                 "Real": GLib.Variant("b", True),
                                 "State": GLib.Variant("u", d["state"]),
                                 "StateReason": GLib.Variant("(uu)", (d["state"], 0)),
                                 "HwAddress": GLib.Variant("s", f"02:00:00:00:00:{n:02x}")}}
        if d["type"] == "wifi":
            ifaces[WIRELESS_IFACE] = {"HwAddress": GLib.Variant("s", f"02:00:00:00:00:{n:02x}"),
                                      "PermHwAddress": GLib.Variant("s", f"02:00:00:00:00:{n:02x}"),
                                      "Mode": GLib.Variant("u", 2),
                                      "WirelessCapabilities": GLib.Variant("u", 0x7ff)}
        elif d["type"] == "ethernet":
            ifaces[WIRED_IFACE] = {"HwAddress": GLib.Variant("s", f"02:00:00:00:00:{n:02x}"),
                                   "PermHwAddress": GLib.Variant("s", f"02:00:00:00:00:{n:02x}")}
        for i, extra in d.get("props", {}).items():
            ifaces.setdefault(i, {}).update(extra)
        silent_add(path, ifaces)

    manager = {"Devices": GLib.Variant("ao", device_paths), "AllDevices": GLib.Variant("ao", device_paths),
               "NetworkingEnabled": GLib.Variant("b", True), "WirelessEnabled": GLib.Variant("b", True),
               "WirelessHardwareEnabled": GLib.Variant("b", True), "Version": GLib.Variant("s", "1.58.0"),
               "Startup": GLib.Variant("b", False),
               # NM_STATE_DISCONNECTED, NM_CONNECTIVITY_NONE
               "State": GLib.Variant("u", 20), "Connectivity": GLib.Variant("u", 1)}
    manager.update(spec.get("manager", {}))
    silent_add(NM_PATH, {NM_IFACE: manager})
    connection_paths = []
    for n, text in enumerate(spec.get("connections", []), 1):
        path = f"{SETTINGS_PATH}/{n}"
        counters["conn"] = n
        settings_store[path] = GLib.Variant.parse(GLib.VariantType.new("a{sa{sv}}"), text, None, None)
        connection_paths.append(path)
        silent_add(path, {CONNECTION_IFACE: {}})
    silent_add(SETTINGS_PATH, {SETTINGS_IFACE: {"Connections": GLib.Variant("ao", connection_paths),
                                                "CanModify": GLib.Variant("b", True)}})

    loop = GLib.MainLoop()

    def acquired(*_):
        print("READY", flush=True)

    def lost(*_):
        print("fake_nm: lost the name", file=sys.stderr)
        loop.quit()

    Gio.bus_own_name_on_connection(conn, NM_NAME, Gio.BusNameOwnerFlags.NONE, acquired, lost)
    loop.run()


if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        serve(json.load(f))
