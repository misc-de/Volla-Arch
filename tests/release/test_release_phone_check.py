# tools/phone-check.py: the live check of the phone's everyday functions. The checks run against
# a stand-in phone that answers the commands they give (busctl, nmcli, mmcli ... as the phone
# answered them when measured), the GPS check against an NMEA server on a Unix socket in
# tmp_path, the command runner and the PC side against stubs (runuser, loginctl, ssh).

import json
import socket
import threading

import pytest

from conftest import TOOLS, load_script

SCRIPT = TOOLS / "phone-check.py"


@pytest.fixture
def pc(tmp_path, monkeypatch):
    mod = load_script(SCRIPT)
    monkeypatch.setattr(mod, "SYS", str(tmp_path / "sys"))
    monkeypatch.setattr(mod, "PROC", str(tmp_path / "proc"))
    monkeypatch.setattr(mod, "RUN", str(tmp_path / "run"))
    for name in ("USR", "ETC", "HOME"):
        monkeypatch.setattr(mod, name, str(tmp_path / name.lower()))
    monkeypatch.setattr(mod, "NMEA_SOCKET", str(tmp_path / "n.sock"))
    return mod


def sv(**props):
    """busctl --json=short output of a call returning a{sv}."""
    return json.dumps({"type": "a{sv}", "data": [
        {k: {"type": "v", "data": v} for k, v in props.items()}]})


def modems_json(*modems):
    return json.dumps({"type": "a(oa{sv})", "data": [[
        [path, {k: {"type": "v", "data": v} for k, v in props.items()}]
        for path, props in modems]]})


FULL_IFACES = ["org.ofono.SimManager", "org.ofono.NetworkRegistration",
               "org.ofono.VoiceCallManager", "org.ofono.MessageManager",
               "org.ofono.IpMultimediaSystem", "org.ofono.ConnectionManager"]

SESSION_NAMES = ["org.freedesktop.DBus", "org.mobian_project.CallAudio", "sm.puri.Chatty",
                 "org.sigxcpu.Feedback", "org.plinius.CameraBridge"]
SYSTEM_NAMES = ["org.freedesktop.DBus", "org.ofono", "org.plinius.Face"]

PW_DUMP = [
    {"type": "PipeWire:Interface:Node", "info": {"state": "suspended", "props": {
        "node.name": n, "media.class": c}}}
    for n, c in (("hal_output", "Audio/Sink"), ("hal_earpiece", "Audio/Sink"),
                 ("hal_input", "Audio/Source"), ("hal_camera_0", "Video/Source"),
                 ("hal_camera_1", "Video/Source"))
] + [{"type": "PipeWire:Interface:Client", "info": {"props": {}}}, {"type": "PipeWire:Interface:Node"}]

DUMPSYS = """== Service global info: ==
Number of camera devices: 2
Number of normal camera devices: 2
  Device 0 is closed, no client instance
  Device 1 is open
"""

GSM_AUTO = "802-11-wireless:yes\ngsm:yes\n"

# vulkaninfo --summary on the phone (vulkan-tools 1.4.363, hybris-vulkan 0.2), from "Devices:"
VULKANINFO = """==========
VULKANINFO
==========

Devices:
========
GPU0:
\tapiVersion         = 1.3.247
\tdriverVersion      = 44.1.0
\tvendorID           = 0x13b5
\tdeviceType         = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
\tdeviceName         = Mali-G615 MC2
\tdriverID           = DRIVER_ID_ARM_PROPRIETARY
\tdriverName         = Mali-G615 MC2
"""
LLVMPIPE = """Devices:
========
GPU0:
\tapiVersion         = 1.4.318
\tdeviceType         = PHYSICAL_DEVICE_TYPE_CPU
\tdeviceName         = llvmpipe (LLVM 21.1.0, 128 bits)
"""

# GL_PROBE's answers as measured on the phone (pbuffer, cleared to 0, 0.5, 1, 1)
GLES_ANSWER = {"vendor": "ARM", "renderer": "Mali-G615 MC2",
               "version": "OpenGL ES 3.2 v1.r44p1-01eac0.148b774f93332d7e562320793fb6c4c9",
               "pixel": [0, 128, 255, 255]}
GL4ES_ANSWER = {"vendor": "GL4ES wrapping ARM", "renderer": "GL4ES using Mali-G615 MC2",
                "version": "2.1 gl4es wrapper 1.1.7", "pixel": [0, 128, 255, 255]}


def probe_out(answer):
    """GL_PROBE's stdout: the driver's chatter around the PROBE line."""
    return "[1.0] (python3) EGL: eglp_unload_callback\nPROBE " + json.dumps(answer) + "\n"


class FakePhone:
    """The phone as the checks see it: answers by command, the real Phone's helpers on top."""

    def __init__(self, mod, tmp_path):
        self.real = mod.Phone  # a test may replace mod.Phone by this stand-in
        self.user, self.uid = "plinius", "32011"
        self._modems = None
        self.calls = []
        self.modem_list = [("/ril_0", {"Online": True, "Powered": True,
                                       "Interfaces": list(FULL_IFACES)}),
                           ("/ril_1", {"Online": False, "Powered": True,
                                       "Interfaces": ["org.ofono.SimManager"]})]
        self.props = {
            ("/ril_0", "org.ofono.SimManager"): {"Present": True, "PinRequired": "none"},
            ("/ril_1", "org.ofono.SimManager"): {"Present": False},
            ("/ril_0", "org.ofono.NetworkRegistration"): {
                "Status": "registered", "Name": "Example", "Technology": "lte", "Strength": 60},
            ("/ril_0", "org.ofono.VoiceCallManager"): {"EmergencyNumbers": ["110", "112"]},
            ("/ril_0", "org.ofono.IpMultimediaSystem"): {"Registered": True, "VoiceCapable": True},
            ("/ril_0", "org.ofono.MessageManager"): {"ServiceCenterAddress": "+491700000000"},
        }
        self.session_names = list(SESSION_NAMES)
        self.system_names = list(SYSTEM_NAMES)
        self.answers = {
            ("plinius-health", "status"): (0, "ok units: no failed system units\n"
                                              "ok container: Android container running\n"),
            ("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"): (0, (
                "wlan0:wifi:connected:Home\\:Net\n/ril_0:gsm:connected:Mobile Broadband\n"
                "lo:loopback:connected (externally):lo\nap0:wifi:disconnected:\n")),
            ("nmcli", "-g", "GENERAL.IP-IFACE", "device", "show", "/ril_0"): (0, "ccmni1\n"),
            ("nmcli", "-t", "-f", "TYPE,AUTOCONNECT", "connection", "show"): (0, GSM_AUTO),
            ("nmcli", "-t", "radio", "wifi"): (0, "enabled\n"),
            ("NetworkManager", "--print-config"): (0, "[main]\nuri=http://wrong.example\n"
                                                      "[connectivity]\n"
                                                      "uri=http://check.example/nm.txt\n"),
            ("curl",): (0, "200 0.412"),
            ("nmcli", "-t", "-f", "BSSID", "device", "wifi"): (
                0, "AA\\:BB\\:CC\\:00\\:00\\:01\nAA\\:BB\\:CC\\:00\\:00\\:02\n"),
            ("bluetoothctl", "show"): (0, "Controller 00:00:00:00:00:00 (public)\n"
                                          "\tPowered: yes\n"),
            ("bluetoothctl", "devices", "Connected"): (0, "Device 00:11:22:33:44:55 Buds\n"),
            ("bluetoothctl", "--timeout"): (124, (
                "Discovery started\n[NEW] Device 00:11:22:33:44:55 Buds\n"
                "[CHG] Device 00:11:22:33:44:66 RSSI: 0xffffffb7 (-73)\n"
                "[CHG] Device 00:11:22:33:44:66 UUIDs: 0000110b\n")),
            ("timeout", "4", "monitor-sensor"): (124, (
                "    Waiting for iio-sensor-proxy to appear\n"
                "=== Has accelerometer (orientation: normal, tilt: vertical)\n"
                "=== Has ambient light sensor (value: 21.000000, unit: lux)\n"
                "=== Has proximity sensor (near: 0)\n")),
            ("pgrep", "-u", "plinius", "-x", "gnome-calls"): (0, "4990\n"),
            ("pw-dump",): (0, json.dumps(PW_DUMP)),
            ("systemctl", "--user", "is-active", "camera-hal-bridge"): (0, "active\n"),
            ("systemctl", "is-active", "plinius-torch"): (0, "active\n"),
            ("lxc-attach",): (0, DUMPSYS),
            ("upower", "-i"): (0, "  native-path:          battery\n    present:             "
                                  "yes\n    state:               discharging\n"
                                  "    percentage:          72%\n"),
            ("fprintd-list", "plinius"): (0, "Fingerprints for user plinius on Biomd (press):\n"
                                             " - #0: right-thumb\n - #1: right-index-finger\n"),
            ("timedatectl",): (0, "yes\n"),
            ("vulkaninfo", "--summary"): (0, VULKANINFO),
        }
        self.gl = {"es": (0, probe_out(GLES_ANSWER)), "gl": (0, probe_out(GL4ES_ANSWER))}
        gl4es = tmp_path / "usr/lib/gl4es"
        gl4es.mkdir(parents=True)
        (gl4es / "libGL.so.1").write_text("")
        (tmp_path / "usr/lib/libvulkan_hybris.so").write_text("")
        icd = tmp_path / "usr/share/vulkan/icd.d"
        icd.mkdir(parents=True)
        (icd / "hybris_icd.aarch64.json").write_text(json.dumps(
            {"file_format_version": "1.0.1", "ICD": {"library_path": "libvulkan_hybris.so"}}))
        envd = tmp_path / "usr/lib/environment.d"
        envd.mkdir(parents=True)
        (envd / "50-plinius-hybris.conf").write_text(
            "# libhybris\nHYBRIS_LD_LIBRARY_PATH=/system/lib64\n")
        self.fprint_devices = ["/net/reactivated/Fprint/Device/0"]
        self.nfc_powered = True
        sysfs = tmp_path / "sys/class/leds"
        for led, files in (("lcd-backlight", {"brightness": "82", "max_brightness": "259"}),
                           ("white:torch", {"max_brightness": "10", "brightness": "0"}),
                           ("vibrator", {"activate": "0"})):
            (sysfs / led).mkdir(parents=True)
            for name, value in files.items():
                (sysfs / led / name).write_text(value + "\n")
        (tmp_path / "proc/bus/input").mkdir(parents=True)
        (tmp_path / "proc/bus/input/devices").write_text(
            'I: Bus=0000\nN: Name="gpio-keys"\n\nN: Name="fts_ts"\n')

    # the real Phone's helpers, on this run()
    def busctl(self, *args, user=False):
        return self.real.busctl(self, *args, user=user)

    def ofono(self, path, iface, method="GetProperties"):
        return self.real.ofono(self, path, iface, method)

    def modems(self):
        self._modems = None  # the tests change modem_list between checks
        return self.real.modems(self)

    def bus_names(self, user=False):
        return self.real.bus_names(self, user=user)

    def run(self, cmd, timeout=15, user=False):
        self.calls.append((tuple(cmd), user))
        if cmd[0] == "busctl":
            return self.bus(cmd[2:], user)
        while "=" in cmd[0]:  # environment in front of the program
            cmd = cmd[1:]
        if cmd[0] == "python3":
            return self.gl[cmd[-1]]
        for n in range(len(cmd), 0, -1):
            if tuple(cmd[:n]) in self.answers:
                return self.answers[tuple(cmd[:n])]
        return 127, ""

    def bus(self, args, user):
        assert args[0] == "--json=short"
        args = args[1:]
        if args[:2] == ["call", "org.freedesktop.DBus"]:
            names = self.session_names if user else self.system_names
            return (0, json.dumps({"type": "as", "data": [names]})) if names is not None \
                else (1, "")
        if args[:5] == ["call", "org.ofono", "/", "org.ofono.Manager", "GetModems"]:
            return 0, modems_json(*self.modem_list)
        if args[:2] == ["call", "org.ofono"]:
            props = self.props.get((args[2], args[3]))
            return (0, sv(**props)) if props is not None else (1, "")
        if args[:2] == ["call", "net.reactivated.Fprint"]:
            return 0, json.dumps({"type": "ao", "data": [self.fprint_devices]})
        if args[:2] == ["get-property", "org.sailfishos.nfc.daemon"]:
            return (0, json.dumps({"type": "b", "data": self.nfc_powered})) \
                if self.nfc_powered is not None else (1, "")
        raise AssertionError(f"unexpected busctl {args}")

    def commands(self, program):
        return [c for c, _ in self.calls if c[0] == program]


@pytest.fixture
def phone(pc, tmp_path):
    return FakePhone(pc, tmp_path)


# --- the healthy phone -----------------------------------------------------------------------

def test_healthy_phone_passes_every_check(pc, phone):
    lines = []
    results = pc.run_checks(phone, pc.NAMES, gps_seconds=0,
                            emit=lambda line, flush: lines.append(json.loads(line)))
    by_name = {r["name"]: r for r in results}
    assert [r["name"] for r in lines] == pc.NAMES  # in the order of CHECKS, though side by side
    assert by_name["gps"]["status"] == "fail"      # no NMEA socket in this world
    del by_name["gps"]
    assert {n: r["status"] for n, r in by_name.items() if r["status"] != "ok"} == {}
    assert by_name["network"]["message"] == "/ril_0: Example, lte, signal 60 %"
    assert by_name["calls"]["message"] == "ready to ring and dial; /ril_0: VoLTE"
    assert by_name["data"]["message"] == \
        "internet through ccmni1 (Mobile Broadband), answer in 0.4 s"
    assert by_name["wifi"]["message"] == "scan sees 2 access point(s), connected, internet in 0.4 s"
    assert by_name["bluetooth"]["message"] == "scan sees 2 device(s), 1 device(s) connected"
    assert by_name["sensors"]["message"] == \
        "accelerometer, light sensor, proximity sensor; light 21 lux"
    assert by_name["camera"]["message"] == "2 cameras in CameraService, bridge running, 1 in use"
    assert by_name["fingerprint"]["message"] == "reader present, 2 finger(s) enrolled"
    assert by_name["display"]["message"] == "touchscreen, backlight 82/259"
    assert by_name["torch"]["message"] == "torch LED with 10 levels"
    assert by_name["opengl"]["message"] == "OpenGL ES 3.2 on Mali-G615 MC2, OpenGL 2.1 through " \
                                           "gl4es, drawn and read back"
    assert by_name["vulkan"]["message"] == "Vulkan 1.3.247 on Mali-G615 MC2"
    assert all(set(r) == {"name", "title", "status", "message", "seconds"} for r in results)


def test_nothing_is_switched_or_sent(pc, phone):
    """Only reading commands, scans and the two HTTP requests: no call, SMS, pairing, sound."""
    pc.run_checks(phone, pc.NAMES, gps_seconds=0, emit=lambda *a, **k: None)
    for cmd, _ in phone.calls:
        if cmd[0] == "busctl":
            assert cmd[3] in ("call", "get-property")
            if cmd[3] == "call":
                assert cmd[-1] in ("GetProperties", "GetModems", "ListNames", "GetDevices")
        assert not {"Dial", "SendMessage", "pair", "connect", "power", "pw-play", "pw-record",
                    "fbcli", "nmcli connection up"} & set(cmd)
    assert all(c[:2] == ("nmcli", "-t") or c[:2] == ("nmcli", "-g") or c[1] == "--print-config"
               for c in phone.commands("nmcli") + phone.commands("NetworkManager"))
    curls = phone.commands("curl")
    assert sorted(c[c.index("--interface") + 1] for c in curls) == ["ccmni1", "wlan0"]
    assert all(c[-1] == "http://check.example/nm.txt" for c in curls)
    assert all(c[c.index("-A") + 1] == "Plinius Arch Linux" for c in curls)


def test_no_scan_leaves_the_scans_out(pc, phone):
    assert pc.check_wifi(phone, scan=False) == ("ok", "connected, internet in 0.4 s")
    assert pc.check_bluetooth(phone, scan=False) == ("ok", "1 device(s) connected, adapter on")
    assert not [c for c in phone.commands("nmcli") if "--rescan" in c]
    assert ("bluetoothctl", "--timeout", "8", "scan", "on") not in phone.commands("bluetoothctl")


def test_session_programs_are_asked_as_the_user(pc, phone):
    pc.check_camera(phone)
    pc.check_calls(phone)
    pc.check_opengl(phone)
    pc.check_vulkan(phone)
    as_user = {next(a for a in c if "=" not in a) for c, user in phone.calls if user}
    assert as_user == {"systemctl", "pw-dump", "busctl", "python3", "vulkaninfo"}
    assert [c for c, user in phone.calls if c[0] == "busctl" and user][0][:2] == \
        ("busctl", "--user")


def test_a_broken_check_does_not_hide_the_others(pc, phone, monkeypatch):
    def broken(phone):
        raise KeyError("x")
    monkeypatch.setattr(pc, "CHECKS", [("sim", "SIM", broken, "")] + pc.CHECKS[2:4])
    results = pc.run_checks(phone, ["sim", "network", "calls"], emit=lambda *a, **k: None)
    assert results[0]["status"] == "fail" and results[0]["message"] == \
        "check broke: KeyError: 'x'"
    assert [r["status"] for r in results[1:]] == ["ok", "ok"]


# --- self-test, SIM, network ------------------------------------------------------------------

@pytest.mark.parametrize("answer, expect", [
    ((127, ""), ("skip", "plinius-health not installed")),
    ((0, "not checked yet\n"), ("skip", "self-test after boot has not run yet")),
    ((2, "ok units: fine\nfail modem: no modem in ModemManager\nfail wifi: no wlan0\n"),
     ("fail", "modem: no modem in ModemManager; wifi: no wlan0")),
    ((0, "ok units: fine\nskip audio: no graphical session\n"), ("ok", "1 layers ok")),
])
def test_selftest(pc, phone, answer, expect):
    phone.answers[("plinius-health", "status")] = answer
    assert pc.check_selftest(phone) == expect


def test_sim_states(pc, phone):
    assert pc.check_sim(phone) == ("ok", "1 SIM(s) ready, online: /ril_0")
    phone.props[("/ril_0", "org.ofono.SimManager")]["PinRequired"] = "pin"
    assert pc.check_sim(phone) == ("fail", "SIM locked: /ril_0: pin")


def test_flight_mode_skips_what_needs_the_network(pc, phone):
    phone.modem_list[0][1]["Online"] = False
    assert pc.check_sim(phone) == ("skip", "1 SIM(s), all modems offline (flight mode?)")
    for check in (pc.check_network, pc.check_calls, pc.check_sms, pc.check_data):
        assert check(phone) == ("skip", "modem offline (flight mode?)")


def test_without_sim_or_modem(pc, phone):
    phone.props[("/ril_0", "org.ofono.SimManager")]["Present"] = False
    assert pc.check_sim(phone) == ("fail", "no SIM card found")
    assert pc.check_network(phone) == ("skip", "no SIM")
    phone.modem_list = []
    assert pc.check_sim(phone) == ("fail", "oFono has no modem (ofono, Android container)")


def test_modem_without_sim_manager_and_unreadable_ofono(pc, phone):
    phone.modem_list = [("/ril_0", {"Online": True, "Interfaces": []})]
    assert pc.check_sim(phone) == ("fail", "no SIM card found")
    phone.answers = {}
    phone.props = {}
    phone.bus = lambda args, user: (0, "not json")
    assert phone.modems() == []
    assert phone.ofono("/ril_0", "org.ofono.SimManager") == {}


def test_network_registration(pc, phone):
    reg = phone.props[("/ril_0", "org.ofono.NetworkRegistration")]
    reg["Status"] = "roaming"
    assert pc.check_network(phone) == ("ok", "/ril_0: Example, lte, signal 60 %, roaming")
    reg["Status"] = "searching"
    assert pc.check_network(phone) == ("fail", "not registered: /ril_0: searching")


def test_network_names_both_sims(pc, phone):
    phone.modem_list[1] = ("/ril_1", {"Online": True, "Interfaces": list(FULL_IFACES)})
    phone.props[("/ril_1", "org.ofono.SimManager")] = {"Present": True, "PinRequired": "none"}
    assert pc.check_network(phone) == ("fail", "not registered: /ril_1: unknown; "
                                               "/ril_0: Example, lte, signal 60 %")


# --- calls, SMS --------------------------------------------------------------------------------

def test_calls_without_volte(pc, phone):
    phone.props[("/ril_0", "org.ofono.IpMultimediaSystem")]["Registered"] = False
    assert pc.check_calls(phone) == ("warn", "/ril_0: no VoLTE on lte, calls drop to 2G/3G")
    phone.props[("/ril_0", "org.ofono.NetworkRegistration")]["Technology"] = "gsm"
    assert pc.check_calls(phone) == ("ok", "ready to ring and dial")


def test_calls_problems(pc, phone):
    phone.props[("/ril_0", "org.ofono.VoiceCallManager")]["EmergencyNumbers"] = ["911"]
    phone.answers[("pgrep", "-u", "plinius", "-x", "gnome-calls")] = (1, "")
    phone.session_names.remove("org.mobian_project.CallAudio")
    status, msg = pc.check_calls(phone)
    assert status == "fail"
    assert msg == ("/ril_0: 112 not among the emergency numbers; GNOME Calls not running: "
                   "incoming calls would not ring; callaudiod missing: no audio routing in "
                   "calls; /ril_0: VoLTE")


def test_calls_without_voice_or_session(pc, phone):
    phone.modem_list[0][1]["Interfaces"].remove("org.ofono.VoiceCallManager")
    phone.user = ""
    assert pc.check_calls(phone) == ("fail", "/ril_0: no voice calls (VoiceCallManager missing); "
                                             "no graphical session: nothing would ring")


def test_sms(pc, phone):
    assert pc.check_sms(phone) == ("ok", "service centre set, Chatty running")
    phone.props[("/ril_0", "org.ofono.MessageManager")]["ServiceCenterAddress"] = ""
    phone.session_names.remove("sm.puri.Chatty")
    assert pc.check_sms(phone) == ("fail", "/ril_0: no SMS service centre, SMS cannot be sent; "
                                           "Chatty not running: incoming SMS would not be shown")
    phone.modem_list[0][1]["Interfaces"].remove("org.ofono.MessageManager")
    phone.user = ""
    assert pc.check_sms(phone) == ("fail", "/ril_0: no SMS (MessageManager missing); "
                                           "no graphical session: SMS would not be shown")


def test_session_bus_unreachable_counts_as_missing(pc, phone):
    phone.session_names = None
    assert phone.bus_names(user=True) == set()
    assert pc.check_sms(phone)[0] == "fail"


# --- mobile data, Wi-Fi, Bluetooth -------------------------------------------------------------

def test_data_switched_off_or_lost(pc, phone):
    phone.answers[("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device")] = (
        0, "/ril_0:gsm:disconnected:\n")
    assert pc.check_data(phone) == ("fail", "mobile data is on but not connected")
    phone.answers[("nmcli", "-t", "-f", "TYPE,AUTOCONNECT", "connection", "show")] = (
        0, "gsm:no\n802-11-wireless:yes\n")
    assert pc.check_data(phone) == ("skip", "mobile data switched off")
    phone.answers[("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device")] = (
        0, "wlan0:wifi:connected:Home\n")
    assert pc.check_data(phone) == ("fail", "NetworkManager has no mobile broadband device "
                                            "(ModemManager, ofono2mm)")


def test_data_without_internet(pc, phone):
    phone.answers[("curl",)] = (28, "000 10.001")
    assert pc.check_data(phone) == ("fail", "no answer through ccmni1 (curl 28, HTTP 000)")
    phone.answers[("curl",)] = (127, "")
    assert pc.check_data(phone) == ("fail", "curl missing")
    phone.answers[("curl",)] = (6, "")
    assert pc.check_data(phone) == ("fail", "no answer through ccmni1 (curl 6, HTTP -)")


def test_data_interface_and_uri_fallbacks(pc, phone):
    del phone.answers[("nmcli", "-g", "GENERAL.IP-IFACE", "device", "show", "/ril_0")]
    del phone.answers[("NetworkManager", "--print-config")]
    assert pc.check_data(phone)[0] == "ok"
    curl = phone.commands("curl")[-1]
    assert curl[curl.index("--interface") + 1] == "/ril_0"
    assert curl[-1] == pc.CONNECTIVITY_URI


def test_wifi(pc, phone):
    phone.answers[("nmcli", "-t", "radio", "wifi")] = (0, "disabled\n")
    assert pc.check_wifi(phone) == ("skip", "Wi-Fi switched off")
    phone.answers[("nmcli", "-t", "radio", "wifi")] = (0, "enabled\n")
    phone.answers[("nmcli", "-t", "-f", "BSSID", "device", "wifi")] = (0, "")
    phone.answers[("curl",)] = (7, "000 0.0")
    assert pc.check_wifi(phone) == ("warn", "scan sees 0 access point(s), connected, but no "
                                            "answer through wlan0 (curl 7, HTTP 000)")
    phone.answers[("nmcli", "-t", "-f", "BSSID", "device", "wifi")] = (10, "")
    assert pc.check_wifi(phone) == ("fail", "scan failed (nmcli 10)")
    phone.answers[("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device")] = (
        0, "wlan0:wifi:disconnected:\n")
    assert pc.check_wifi(phone, scan=False) == ("ok", "not connected")
    phone.answers[("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device")] = (
        0, "ap0:wifi:disconnected:\n")
    assert pc.check_wifi(phone) == ("fail", "no Wi-Fi device wlan0 in NetworkManager")


def test_bluetooth(pc, phone):
    phone.answers[("bluetoothctl", "--timeout")] = (124, "Discovery started\n")
    phone.answers[("bluetoothctl", "devices", "Connected")] = (0, "")
    assert pc.check_bluetooth(phone) == ("warn", "scan sees 0 device(s)")
    phone.answers[("bluetoothctl", "show")] = (0, "Controller 00:00 (public)\n\tPowered: no\n")
    assert pc.check_bluetooth(phone) == ("skip", "Bluetooth switched off")
    phone.answers[("bluetoothctl", "show")] = (1, "No default controller available\n")
    assert pc.check_bluetooth(phone) == ("fail", "BlueZ has no adapter (bluebinder)")
    del phone.answers[("bluetoothctl", "show")]
    assert pc.check_bluetooth(phone) == ("fail", "bluetoothctl missing")


# --- GPS ---------------------------------------------------------------------------------------

def nmea_server(path, chunks, close=True):
    """Serves chunks to the first client on a Unix socket; returns the thread."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        try:
            for c in chunks:
                conn.sendall(c)
            if not close:
                conn.recv(1)  # until the client hangs up
        except OSError:  # the client hung up early (after a fix)
            pass
        conn.close()
        srv.close()
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


GSV_NO_SIGNAL = (b"$GPGSV,2,1,05,03,03,111,,04,15,270,,06,01,010,,13,06,292,,1*74\r\n"
                 b"$GPGSV,2,2,05,17,40,100,,1*70\r\n")
GSV_SIGNAL = (b"$GLGSV,1,1,03,78,76,239,31,88,74,244,,87,42,044,22*43\r\n"
              b"$GBGSV,1,1,01,25,56,268,18*73\r\n")


def test_gps_fix(pc, phone, tmp_path):
    t = nmea_server(tmp_path / "n.sock", [
        b"$PMTKA,1\r\n", GSV_SIGNAL[:20], GSV_SIGNAL[20:],
        b"$GNGGA,101010.00,5000.0,N,00800.0,E,1,07,1.2,100.0,M,,,,*5C\r\n",
        b"$GNRMC,never,read\r\n"], close=False)
    status, msg = pc.check_gps(phone, seconds=10)
    t.join(5)
    assert status == "ok" and msg.startswith("fix after ") and msg.endswith("with 7 satellites")


def test_gps_satellites_heard_without_fix(pc, phone, tmp_path):
    t = nmea_server(tmp_path / "n.sock", [GSV_SIGNAL, GSV_NO_SIGNAL, b"no nmea\n",
                                          b"$GNGGA,101010.00,,,,,0,00,,,M,,,,*5C\r\n"])
    assert pc.check_gps(phone, seconds=5) == (
        "ok", "3 satellite(s) heard (best 31 dB-Hz), no fix in 5 s (indoors?)")
    t.join(5)


def test_gps_indoors_and_silent(pc, phone, tmp_path):
    t = nmea_server(tmp_path / "n.sock", [GSV_NO_SIGNAL], close=False)
    assert pc.check_gps(phone, seconds=0.5) == (
        "warn", "receiver runs, 5 satellite(s) expected in view, none heard in 0.5 s (indoors?)")
    t.join(5)
    (tmp_path / "n.sock").unlink()
    t = nmea_server(tmp_path / "n.sock", [])
    assert pc.check_gps(phone, seconds=2) == (
        "fail", "no NMEA sentence in 2 s (GNSS HAL, Android container)")
    t.join(5)


def test_gps_without_bridge(pc, phone, tmp_path):
    assert pc.check_gps(phone) == ("fail", "no NMEA socket (gnss-nmea-bridge)")
    (tmp_path / "n.sock").write_text("")  # a stale file, nobody listening
    status, msg = pc.check_gps(phone)
    assert status == "fail" and msg.startswith("NMEA socket: ")


def test_gps_timeout_before_first_read(pc, phone, tmp_path):
    t = nmea_server(tmp_path / "n.sock", [], close=False)
    assert pc.check_gps(phone, seconds=0)[0] == "fail"
    t.join(5)


def test_nmea_parsing():
    sats = {}
    pc_mod = load_script(SCRIPT)
    pc_mod.gsv_satellites("$GPGSV,1,1,02,03,03,111,40,,15,270,,*74", sats)
    pc_mod.gsv_satellites("$GPGSV,1,1,01,03,03,111,12*74", sats)  # the best signal stays
    assert sats == {("GP", "03"): 40}
    assert pc_mod.gga_fix("$GPGGA,1,2,N,3,E,2,,1.0*00") == 0
    assert pc_mod.gga_fix("$GPGGA,1,2,N,3,E,0,05,1.0*00") is None
    assert pc_mod.gga_fix("$GPGGA,short") is None


# --- sensors, cameras, audio, display ----------------------------------------------------------

def test_sensors(pc, phone):
    phone.answers[("timeout", "4", "monitor-sensor")] = (124, (
        "=== Has accelerometer (orientation: undefined, tilt: vertical)\n"
        "=== No ambient light sensor\n=== No proximity sensor\n"))
    assert pc.check_sensors(phone) == ("fail", "missing: light sensor, proximity sensor "
                                               "(sensor-proxy-bridge)")
    phone.answers[("timeout", "4", "monitor-sensor")] = (124, (
        "=== Has accelerometer\n=== Has ambient light sensor (value: n/a)\n"
        "=== Has proximity sensor (near: 1)\n"))
    assert pc.check_sensors(phone) == ("ok", "accelerometer, light sensor, proximity sensor")
    del phone.answers[("timeout", "4", "monitor-sensor")]
    assert pc.check_sensors(phone) == ("fail", "monitor-sensor missing (iio-sensor-proxy)")


def test_camera_nodes_hidden_from_native_clients(pc, phone):
    """WirePlumber hides the camera nodes from pw-dump once it has set its permissions."""
    phone.answers[("pw-dump",)] = (0, json.dumps(PW_DUMP[:3]))
    assert pc.check_camera(phone) == ("ok", "2 cameras in CameraService, bridge running, 1 in use")


def test_camera_problems(pc, phone):
    dump = json.loads(json.dumps(PW_DUMP))
    dump[3]["info"]["state"] = "error"
    phone.answers[("pw-dump",)] = (0, json.dumps(dump))
    assert pc.check_camera(phone) == ("fail", "camera node(s) in error: hal_camera_0")
    phone.answers[("pw-dump",)] = (0, json.dumps(PW_DUMP[:4]))
    assert pc.check_camera(phone) == ("fail", "CameraService has 2 camera(s), PipeWire 1 node(s)")
    phone.answers[("lxc-attach",)] = (1, "Can't find service: media.camera\n")
    assert pc.check_camera(phone) == ("fail", "CameraService in the Android container reports "
                                              "no camera")
    phone.session_names.remove("org.plinius.CameraBridge")
    assert pc.check_camera(phone) == ("fail", "camera-hal-bridge runs but is not on the session "
                                              "bus")
    phone.answers[("systemctl", "--user", "is-active", "camera-hal-bridge")] = (3, "failed\n")
    assert pc.check_camera(phone) == ("fail", "camera-hal-bridge is failed")
    phone.answers[("systemctl", "--user", "is-active", "camera-hal-bridge")] = (1, "")
    assert pc.check_camera(phone) == ("fail", "camera-hal-bridge is not running")
    phone.user = ""
    assert pc.check_camera(phone) == ("skip", "no graphical session")


def test_audio(pc, phone):
    assert pc.check_audio(phone) == ("ok", "speaker, earpiece and microphone in PipeWire")
    dump = json.loads(json.dumps(PW_DUMP))
    dump[0]["info"]["state"] = "error"
    phone.answers[("pw-dump",)] = (0, json.dumps(dump[:1] + dump[2:]))
    assert pc.check_audio(phone) == ("fail", "missing: earpiece (audio-hal-bridge); "
                                             "in error: speaker")
    phone.answers[("pw-dump",)] = (1, "")
    assert pc.check_audio(phone) == ("fail", "missing: speaker, earpiece, microphone "
                                             "(audio-hal-bridge)")
    phone.answers[("pw-dump",)] = (0, "[{broken")
    assert pc.check_audio(phone)[0] == "fail"
    phone.user = ""
    assert pc.check_audio(phone) == ("skip", "no graphical session")


def test_display(pc, phone, tmp_path):
    (tmp_path / "sys/class/leds/lcd-backlight/brightness").write_text("0\n")
    assert pc.check_display(phone) == ("ok", "touchscreen, screen off")
    (tmp_path / "proc/bus/input/devices").write_text('N: Name="gpio-keys"\n')
    (tmp_path / "sys/class/leds/lcd-backlight/max_brightness").unlink()
    assert pc.check_display(phone) == ("fail", "no touchscreen (fts_ts); no backlight "
                                               "(lcd-backlight)")



# --- OpenGL, Vulkan ----------------------------------------------------------------------------

def test_opengl_gets_the_session_environment(pc, phone, tmp_path):
    assert pc.check_opengl(phone)[0] == "ok"
    es, gl = [c for c, _ in phone.calls if "python3" in c]
    lib = tmp_path / "usr/lib"
    assert es[0] == "HYBRIS_LD_LIBRARY_PATH=/system/lib64"
    assert es[1:5] == ("python3", "-I", "-c", pc.GL_PROBE)
    assert es[5:] == (f"{lib}/libEGL.so.1", f"{lib}/libGLESv2.so.2", "es")
    # as /usr/bin/gl4es starts a program
    assert gl[1:5] == (f"LD_LIBRARY_PATH={lib}/gl4es", f"LIBGL_EGL={lib}/libEGL.so.1",
                       f"LIBGL_GLES={lib}/libGLESv2.so.2", "LIBGL_NOBANNER=1")
    assert gl[-3:] == (f"{lib}/gl4es/libEGL.so.1", f"{lib}/gl4es/libGL.so.1", "gl")


def test_opengl_without_gl4es_or_session(pc, phone, tmp_path):
    (tmp_path / "usr/lib/gl4es/libGL.so.1").unlink()
    assert pc.check_opengl(phone) == ("ok", "OpenGL ES 3.2 on Mali-G615 MC2, drawn and read back")
    assert len([c for c, _ in phone.calls if "python3" in c]) == 1
    phone.user = ""
    assert pc.check_opengl(phone) == ("skip", "no graphical session")


def test_opengl_problems(pc, phone):
    phone.gl["es"] = (0, probe_out({"error": "eglCreateContext failed (EGL error 0x3009)"}))
    phone.gl["gl"] = (0, probe_out({**GL4ES_ANSWER, "pixel": [0, 0, 0, 255]}))
    assert pc.check_opengl(phone) == ("fail", "OpenGL ES: eglCreateContext failed (EGL error "
                                              "0x3009); OpenGL through gl4es draws a wrong colour "
                                              "(0, 0, 0, 255 instead of 0, 128, 255, 255)")
    phone.gl["es"] = (0, probe_out({**GLES_ANSWER, "renderer": "llvmpipe (LLVM 21.1.0)"}))
    phone.gl["gl"] = (0, probe_out({**GL4ES_ANSWER, "pixel": None}))
    assert pc.check_opengl(phone) == ("fail", "OpenGL ES renders in software (llvmpipe (LLVM "
                                              "21.1.0)); OpenGL through gl4es draws a wrong colour "
                                              "(0, 0, 0, 0 instead of 0, 128, 255, 255)")


@pytest.mark.parametrize("answer, error", [
    ((124, "PROBE {\n"), "no answer within 30 s"),           # hung after a torn line
    ((139, "library \"libz.so\" not found\n"), "probe ended with status 139 and no answer"),
    ((127, ""), "probe ended with status 127 and no answer"),
])
def test_opengl_probe_without_answer(pc, phone, answer, error):
    phone.gl["es"] = answer
    assert pc.check_opengl(phone) == ("fail", f"OpenGL ES: {error}")


def test_session_env(pc, tmp_path):
    usr, etc = tmp_path / "usr/lib/environment.d", tmp_path / "etc/environment.d"
    usr.mkdir(parents=True)
    etc.mkdir(parents=True)
    (usr / "50-a.conf").write_text("# comment\nA=1\nB=usr\n\nnot an assignment\n"
                                   "#C=commented\n1X=bad key\n")
    (usr / "README").write_text("D=not a .conf\n")
    (etc / "60-b.conf").write_text("B=etc\nGLIBC_TUNABLES=glibc.rtld.optional_static_tls=4096\n")
    assert pc.session_env() == ["A=1", "B=etc", "GLIBC_TUNABLES=glibc.rtld.optional_static_tls=4096"]


def test_session_env_expands_like_systemd(pc, tmp_path):
    """The phone's files, as they are: taken literally, XDG_DATA_DIRS hid the Vulkan driver."""
    envd = tmp_path / "usr/lib/environment.d"
    envd.mkdir(parents=True)
    (envd / "50-plinius-callaudio.conf").write_text(
        "XDG_DATA_DIRS=/usr/share/plinius:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}\n")
    (envd / "50-plinius-widevine.conf").write_text(
        "MOZ_GMP_PATH=$MOZ_GMP_PATH${MOZ_GMP_PATH:+:}/var/lib/gmp\n")
    (envd / "60-more.conf").write_text("MOZ_GMP_PATH=$MOZ_GMP_PATH${MOZ_GMP_PATH:+:}/opt/gmp\n"
                                       "X=${XDG_DATA_DIRS:-unused}|${UNSET}|${MOZ_GMP_PATH:+set}\n"
                                       "Y=${UNSET:+alternate}$UNSET.\n")
    env = dict(e.split("=", 1) for e in pc.session_env())
    assert env["XDG_DATA_DIRS"] == "/usr/share/plinius:/usr/local/share:/usr/share"
    assert env["MOZ_GMP_PATH"] == "/var/lib/gmp:/opt/gmp"
    assert env["X"] == "/usr/share/plinius:/usr/local/share:/usr/share||set"
    assert env["Y"] == "."


def test_vulkan(pc, phone, tmp_path):
    phone.answers[("vulkaninfo", "--summary")] = (0, VULKANINFO + LLVMPIPE.split("========\n")[1]
                                                  .replace("GPU0", "GPU1"))
    assert pc.check_vulkan(phone) == ("ok", "Vulkan 1.3.247 on Mali-G615 MC2")
    phone.answers[("vulkaninfo", "--summary")] = (0, LLVMPIPE)
    assert pc.check_vulkan(phone) == ("fail", "only a software device: llvmpipe (LLVM 21.1.0, "
                                              "128 bits)")
    phone.answers[("vulkaninfo", "--summary")] = (1, "ERROR: [Loader Message] Code 0 : "
                                                     "vkCreateInstance: Found no drivers!\n")
    assert pc.check_vulkan(phone) == ("fail", "the Vulkan loader finds no device (status 1)")
    phone.answers[("vulkaninfo", "--summary")] = (0, "Devices:\n========\nGPU0:\n"
                                                     "\tapiVersion = 1.3.0\n")  # cut short
    assert pc.check_vulkan(phone) == ("fail", "the Vulkan loader finds no device")
    del phone.answers[("vulkaninfo", "--summary")]
    assert pc.check_vulkan(phone) == ("warn", "1 driver(s) installed, device not checked "
                                              "(vulkaninfo missing: vulkan-tools)")
    phone.user = ""
    assert pc.check_vulkan(phone) == ("skip", "no graphical session")


def test_vulkan_drivers(pc, phone, tmp_path):
    icd = tmp_path / "usr/share/vulkan/icd.d"
    (tmp_path / "usr/lib/libvulkan_hybris.so").unlink()
    (icd / "README").write_text("")
    assert pc.check_vulkan(phone) == ("fail", "driver manifest without its library: "
                                              "hybris_icd.aarch64.json")
    lib = tmp_path / "opt/libvk.so"
    lib.parent.mkdir()
    lib.write_text("")
    (icd / "hybris_icd.aarch64.json").write_text(json.dumps({"ICD": {"library_path": str(lib)}}))
    etc = tmp_path / "etc/vulkan/icd.d"
    etc.mkdir(parents=True)
    (etc / "broken.json").write_text("{not json")
    assert pc.check_vulkan(phone) == ("fail", "driver manifest without its library: broken.json")
    (etc / "broken.json").unlink()
    assert pc.check_vulkan(phone)[0] == "ok"
    (icd / "hybris_icd.aarch64.json").unlink()
    assert pc.check_vulkan(phone) == ("skip", "no Vulkan driver installed (hybris-vulkan)")
    assert not phone.commands("vulkaninfo")


def test_vulkan_launcher_with_a_missing_driver(pc, phone, tmp_path):
    """The Vulkan Cube and Benchmark launchers kept a manifest path that had gone."""
    manifest = tmp_path / "usr/share/vulkan/icd.d/hybris_icd.aarch64.json"
    gone = tmp_path / "var/lib/hybris-vulkan-off/hybris_icd.aarch64.json"
    apps = tmp_path / "home/plinius/.local/share/applications"
    apps.mkdir(parents=True)
    (tmp_path / "home/other").mkdir()        # a user without own launchers
    (tmp_path / "home/.hidden-file").write_text("")
    (apps / "cube.desktop").write_text(
        f"[Desktop Entry]\nName=Vulkan Cube\nExec=env VK_DRIVER_FILES={gone} vkcube --wsi "
        "wayland\n")
    (apps / "ok.desktop").write_text(f"[Desktop Entry]\nExec=env VK_ICD_FILENAMES={manifest} "
                                     "vkmark\nTryExec=VK_DRIVER_FILES=/nowhere\n")
    (apps / "notes.txt").write_text(f"Exec=env VK_DRIVER_FILES={gone} x\n")
    system = tmp_path / "usr/share/applications"
    system.mkdir(parents=True)
    (system / "bench.desktop").write_text(f"Exec=env VK_ADD_DRIVER_FILES={manifest}:{gone} vkmark\n")
    assert pc.check_vulkan(phone) == ("fail", "launcher(s) point at a missing Vulkan driver: "
                                              f"bench.desktop ({gone}); cube.desktop ({gone})")
    assert not phone.commands("vulkaninfo")
    (apps / "cube.desktop").write_text("Exec=vkcube --wsi wayland\n")
    (system / "bench.desktop").unlink()
    assert pc.check_vulkan(phone)[0] == "ok"


def test_no_home_directory(pc, phone, tmp_path):
    assert not (tmp_path / "home").exists()
    assert pc.stale_vulkan_launchers() == []


# --- the probe itself, against a stand-in EGL and GL library ------------------------------------

FAKE_GL = r"""
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int err = 0x3000;
static float clear[4];
static unsigned char cleared[4];

static int fails(const char *fn)
{
    const char *f = getenv("FAKE_GL_FAIL");
    if (f && !strcmp(f, fn)) {
        err = 0x3009;
        return 1;
    }
    return 0;
}

void *eglGetDisplay(void *native) { return fails("eglGetDisplay") ? NULL : (void *)1; }
int eglInitialize(void *d, int *major, int *minor)
{
    *major = 1;
    *minor = 5;
    return !fails("eglInitialize");
}
int eglBindAPI(int api) { return api == 0x30A2 && !fails("eglBindAPI"); }
int eglChooseConfig(void *d, const int *attrs, void **cfg, int size, int *n)
{
    int want = 0;
    for (; *attrs != 0x3038; attrs += 2)
        if (attrs[0] == 0x3040)
            want = attrs[1];
    *n = fails("eglChooseConfig-none") ? 0 : 1;
    *cfg = (void *)(long)want;
    return !fails("eglChooseConfig");
}
void *eglCreatePbufferSurface(void *d, void *cfg, const int *attrs)
{
    return fails("eglCreatePbufferSurface") ? NULL : (void *)2;
}
void *eglCreateContext(void *d, void *cfg, void *share, const int *attrs)
{
    return fails("eglCreateContext") ? NULL : (void *)3;
}
int eglMakeCurrent(void *d, void *draw, void *read, void *ctx)
{
    return ctx == NULL || !fails("eglMakeCurrent");
}
int eglGetError(void) { return err; }
int eglTerminate(void *d)
{
    const char *mark = getenv("FAKE_GL_TERMINATED");
    if (mark)
        fclose(fopen(mark, "w"));
    return 1;
}

const char *glGetString(int name)
{
    const char *r = getenv("FAKE_GL_RENDERER");
    switch (name) {
    case 0x1F00: return "Fake";
    case 0x1F01: return r ? r : "Fake GPU";
    case 0x1F02: return "OpenGL ES 3.2 fake";
    }
    return NULL;
}
void glClearColor(float r, float g, float b, float a)
{
    clear[0] = r; clear[1] = g; clear[2] = b; clear[3] = a;
}
void glClear(int mask)
{
    if (mask == 0x4000 && !getenv("FAKE_GL_NO_DRAW"))
        for (int i = 0; i < 4; i++)
            cleared[i] = (unsigned char)lroundf(clear[i] * 255);
}
void glReadPixels(int x, int y, int w, int h, int format, int type, void *data)
{
    if (format == 0x1908 && type == 0x1401)
        memcpy(data, cleared, 4);
}
"""


@pytest.fixture(scope="module")
def fake_gl(tmp_path_factory):
    from conftest import compile_c
    d = tmp_path_factory.mktemp("fakegl")
    (d / "fakegl.c").write_text(FAKE_GL)
    return compile_c(d / "libfakegl.so", [d / "fakegl.c"], flags=["-shared", "-fPIC", "-lm"],
                     counted=False)


def run_probe(pc, lib, api="es", **env):
    import os
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-I", "-c", pc.GL_PROBE, str(lib), str(lib), api],
                       capture_output=True, text=True, env={**os.environ, **env}, timeout=30)
    assert r.returncode == 0, r.stderr
    line, = [l for l in r.stdout.splitlines() if l.startswith("PROBE ")]
    return json.loads(line[6:])


def test_probe_draws_and_reads_back(pc, fake_gl):
    assert run_probe(pc, fake_gl) == {"vendor": "Fake", "renderer": "Fake GPU",
                                      "version": "OpenGL ES 3.2 fake", "pixel": [0, 128, 255, 255]}
    assert pc.gl_problem("OpenGL ES", run_probe(pc, fake_gl, "gl")) is None
    answer = run_probe(pc, fake_gl, FAKE_GL_NO_DRAW="1", FAKE_GL_RENDERER="llvmpipe")
    assert answer["pixel"] == [0, 0, 0, 0] and answer["renderer"] == "llvmpipe"


def test_probe_terminates_the_display(pc, fake_gl, tmp_path):
    mark = tmp_path / "terminated"
    run_probe(pc, fake_gl, FAKE_GL_TERMINATED=str(mark))
    assert mark.exists()
    mark.unlink()
    run_probe(pc, fake_gl, FAKE_GL_FAIL="eglCreateContext", FAKE_GL_TERMINATED=str(mark))
    assert mark.exists()  # also when a later step failed


@pytest.mark.parametrize("step, api, error", [
    ("eglGetDisplay", "es", "eglInitialize failed (EGL error 0x3009)"),
    ("eglInitialize", "es", "eglInitialize failed (EGL error 0x3009)"),
    ("eglBindAPI", "gl", "eglBindAPI(OpenGL) failed (EGL error 0x3009)"),
    ("eglChooseConfig", "es", "eglChooseConfig failed (EGL error 0x3009)"),
    ("eglChooseConfig-none", "es", "eglChooseConfig failed (EGL error 0x3009)"),
    ("eglCreatePbufferSurface", "es", "eglCreatePbufferSurface failed (EGL error 0x3009)"),
    ("eglCreateContext", "gl", "eglCreateContext failed (EGL error 0x3009)"),
    ("eglMakeCurrent", "es", "eglMakeCurrent failed (EGL error 0x3009)"),
])
def test_probe_names_the_step_that_failed(pc, fake_gl, step, api, error):
    assert run_probe(pc, fake_gl, api, FAKE_GL_FAIL=step) == {"error": error}


def test_probe_without_the_library(pc, tmp_path):
    answer = run_probe(pc, tmp_path / "libnothing.so")
    assert answer["error"].startswith("cannot load: ") and "libnothing.so" in answer["error"]


# --- battery, fingerprint, face, NFC, torch, vibration, clock, storage -------------------------

def test_battery(pc, phone):
    phone.answers[("upower", "-i")] = (0, "  present: yes\n  state: discharging\n"
                                          "  percentage: 9,5%\n")
    assert pc.check_battery(phone) == ("warn", "10 %, discharging")
    phone.answers[("upower", "-i")] = (0, "  present: yes\n  state: charging\n"
                                          "  percentage: 9%\n")
    assert pc.check_battery(phone) == ("ok", "9 %, charging")
    phone.answers[("upower", "-i")] = (0, "  present: no\n")
    assert pc.check_battery(phone) == ("fail", "UPower sees no battery")


def test_fingerprint_and_face(pc, phone):
    phone.user = ""
    assert pc.check_fingerprint(phone) == ("ok", "reader present")
    phone.fprint_devices = []
    assert pc.check_fingerprint(phone) == ("fail", "fprintd has no reader (biomd)")
    assert pc.check_face(phone) == ("ok", "face unlock service running")
    phone.system_names.remove("org.plinius.Face")
    assert pc.check_face(phone) == ("fail", "face unlock service missing (org.plinius.Face)")


def test_nfc(pc, phone):
    assert pc.check_nfc(phone) == ("ok", "adapter present, powered")
    phone.nfc_powered = False
    assert pc.check_nfc(phone) == ("ok", "adapter present, not powered")
    phone.nfc_powered = None
    assert pc.check_nfc(phone) == ("fail", "no NFC adapter (nfcd)")


def test_torch_and_vibration(pc, phone, tmp_path):
    phone.answers[("systemctl", "is-active", "plinius-torch")] = (3, "inactive\n")
    assert pc.check_torch(phone) == ("fail", "plinius-torch is inactive")
    phone.answers[("systemctl", "is-active", "plinius-torch")] = (3, "")
    assert pc.check_torch(phone) == ("fail", "plinius-torch is not running")
    (tmp_path / "sys/class/leds/white:torch/max_brightness").write_text("0\n")
    assert pc.check_torch(phone) == ("fail", "no torch LED (white:torch)")
    assert pc.check_vibration(phone) == ("ok", "motor present, feedbackd running")
    phone.session_names.remove("org.sigxcpu.Feedback")
    assert pc.check_vibration(phone) == ("fail", "feedbackd not running: no vibration or "
                                                 "notification sounds")
    phone.user = ""
    assert pc.check_vibration(phone) == ("ok", "motor present")
    (tmp_path / "sys/class/leds/vibrator/activate").unlink()
    (tmp_path / "sys/class/leds/vibrator").rmdir()
    assert pc.check_vibration(phone) == ("fail", "no vibration motor (leds/vibrator)")


def test_clock(pc, phone):
    assert pc.check_time(phone) == ("ok", "clock synchronised")
    phone.answers[("timedatectl",)] = (0, "no\n")
    assert pc.check_time(phone) == ("warn", "clock not synchronised (no network time yet?)")


@pytest.mark.parametrize("avail, status", [(50, "ok"), (4, "warn"), (1, "fail")])
def test_storage(pc, phone, monkeypatch, avail, status):
    class St:
        f_frsize, f_blocks, f_bavail = 2**20, 100 * 1024, avail * 1024

    def statvfs(path):
        if path == "/home":
            raise FileNotFoundError(path)
        return St
    monkeypatch.setattr(pc.os, "statvfs", statvfs)
    assert pc.check_storage(phone) == (status, f"/ {avail:.1f} GiB free ({avail} %)")


# --- the command runner on the phone -----------------------------------------------------------

def test_phone_runner(pc, fake_bin, monkeypatch, tmp_path):
    fake_bin.stub("loginctl", '''
case $1 in
list-sessions) printf "\\n3 0 root - pts/0\\nc1 32011 plinius seat0 tty7\\n" ;;
show-session) case "$2 $4" in
    "c1 Seat") echo seat0 ;; "c1 Type") echo wayland ;; "c1 Name") echo plinius ;;
    "3 Seat") echo ;; "3 Type") echo tty ;; "3 Name") echo root ;; esac ;;
esac''')
    fake_bin.stub("id", 'echo 32011')
    fake_bin.stub("runuser", 'while [ "$1" != -- ]; do shift; done; shift; exec "$@"')
    fake_bin.stub("busctl", 'echo "{\\"type\\":\\"s\\",\\"data\\":\\"$XDG_RUNTIME_DIR\\"}"')
    fake_bin.stub("slow", 'echo partial; exec sleep 5')
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    phone = pc.Phone()
    assert (phone.user, phone.uid) == ("plinius", "32011")
    assert phone.busctl("get-property", "x", user=True) == f"{tmp_path}/run/user/32011"
    assert fake_bin.calls("runuser")[0][:3] == ["-u", "plinius", "--"]
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=" + f"{tmp_path}/run/user/32011/bus" in \
        fake_bin.calls("runuser")[0]
    assert phone.run(["slow"], timeout=0.5) == (124, "partial\n")
    assert phone.run(["no-such-program"]) == (127, "")


def test_phone_without_session(pc, fake_bin, monkeypatch):
    fake_bin.stub("loginctl", 'exit 1')
    fake_bin.stub("busctl", 'echo "{}"')
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    phone = pc.Phone()
    assert phone.user == "" and phone.uid is None
    assert phone.run(["busctl"], user=True) == (1, "")
    assert phone.busctl("call") is None
    assert fake_bin.calls("runuser") == []


def test_phone_user_without_id(pc, fake_bin, monkeypatch):
    fake_bin.stub("loginctl", 'case $1 in list-sessions) echo "c1 1 x seat0" ;; '
                  'show-session) case $4 in Seat) echo seat0 ;; Type) echo wayland ;; '
                  'Name) echo ghost ;; esac ;; esac')
    fake_bin.stub("id", 'exit 1')
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    assert pc.Phone().uid is None


def test_timeout_output_as_bytes(pc, monkeypatch):
    def run(*a, **k):
        raise pc.subprocess.TimeoutExpired("x", 1, output=b"half\xff")
    monkeypatch.setattr(pc.subprocess, "run", run)
    phone = object.__new__(pc.Phone)
    phone.user = ""
    assert phone.run(["x"]) == (124, "half�")


def test_read_missing_file(pc, tmp_path):
    assert pc.read(tmp_path / "none", "d") == "d"


# --- PC side and command line ------------------------------------------------------------------

def ssh_stub(fake_bin, tmp_path, lines, rc=0):
    out = tmp_path / "ssh-out"
    out.write_text("".join(line + "\n" for line in lines))
    fake_bin.stub("ssh", f'cat > "{tmp_path}/ssh-stdin"; cat "{out}"; exit {rc}')


def results(*statuses):
    return [json.dumps({"name": f"c{i}", "title": f"Check {i}", "status": s,
                        "message": f"message {i}", "seconds": 0.1})
            for i, s in enumerate(statuses)]


def test_from_pc_sends_itself_and_renders(pc, fake_bin, tmp_path, monkeypatch, capsys):
    ssh_stub(fake_bin, tmp_path, results("ok", "warn", "skip") + ["Warning: motd line"])
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    monkeypatch.delenv("PHONE", raising=False)
    monkeypatch.delenv("PHONE_SSH_OPTS", raising=False)
    assert pc.main(["--only", "sim,network", "--skip", "network", "--no-scan",
                    "--gps-seconds", "7"], source="SOURCE") == 0
    call = fake_bin.calls("ssh")[0]
    assert call[:-2] == pc.shlex.split(pc.DEFAULT_SSH_OPTS)
    assert call[-2] == "root@10.15.19.82"
    assert call[-1] == "python3 -I - --on-phone --only sim --gps-seconds 7 --no-scan"
    assert (tmp_path / "ssh-stdin").read_text() == "SOURCE"
    out, err = capsys.readouterr()
    assert out.splitlines() == ["OK    Check 0         message 0", "WARN  Check 1         message 1",
                                "SKIP  Check 2         message 2", "",
                                "1 ok, 1 warnings, 0 failed, 1 skipped"]
    assert err == "Warning: motd line\n"


def test_from_pc_failure_json_and_host(pc, fake_bin, tmp_path, monkeypatch, capsys):
    ssh_stub(fake_bin, tmp_path, results("ok", "fail"), rc=2)
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    monkeypatch.setenv("PHONE", "root@phone.example")
    monkeypatch.setenv("PHONE_SSH_OPTS", "-o Port=2222")
    assert pc.main(["--json"], source="S") == 2
    call = fake_bin.calls("ssh")[0]
    assert call[:3] == ["-o", "Port=2222", "root@phone.example"]
    assert "--only " + ",".join(pc.NAMES) in call[-1]
    assert [json.loads(line)["status"] for line in capsys.readouterr().out.splitlines()] == \
        ["ok", "fail"]
    assert pc.main(["--host", "root@192.0.2.7", "--json"], source="S") == 2
    assert fake_bin.calls("ssh")[1][2] == "root@192.0.2.7"


def test_from_pc_unreachable(pc, fake_bin, tmp_path, monkeypatch, capsys):
    ssh_stub(fake_bin, tmp_path, [], rc=255)
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    assert pc.main(["--host", "root@192.0.2.7"], source="S") == 1
    assert "no answer from root@192.0.2.7 (ssh 255)" in capsys.readouterr().err


def test_from_pc_color_on_a_terminal(pc, monkeypatch):
    assert pc.render({"status": "fail", "title": "SIM", "message": "m"}, True) == \
        "\033[31mFAIL\033[0m  SIM             m"


def test_script_runs_from_stdin_as_ssh_gives_it(fake_bin, tmp_path, monkeypatch, capfd):
    """The remote command as ssh runs it: this file on stdin of python3 -I -, not as root here."""
    pc = load_script(SCRIPT)
    fake_bin.stub("ssh", 'eval "${@: -1}"')
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    if pc.os.geteuid() == 0:
        pytest.skip("runs as root")
    assert pc.main(["--host", "root@192.0.2.7", "--only", "time"]) == 1
    err = capfd.readouterr().err
    assert "phone-check: must run as root on the phone" in err
    assert "no answer from root@192.0.2.7 (ssh 1)" in err


def test_on_phone(pc, phone, monkeypatch, capsys):
    monkeypatch.setattr(pc.os, "geteuid", lambda: 0)
    monkeypatch.setattr(pc, "Phone", lambda: phone)
    assert pc.main(["--on-phone", "--only", "sim,time", "--no-scan"]) == 0
    assert [json.loads(line)["name"] for line in capsys.readouterr().out.splitlines()] == \
        ["sim", "time"]
    phone.answers[("timedatectl",)] = (0, "no\n")
    phone.props[("/ril_0", "org.ofono.SimManager")]["PinRequired"] = "puk"
    assert pc.main(["--on-phone", "--only", "sim,time"]) == 2


def test_list_and_unknown_names(pc, capsys):
    assert pc.main(["--list"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in out] == pc.NAMES
    assert out[1].split(None, 2)[1:] == ["SIM", "SIM present and unlocked, modem online"]
    with pytest.raises(SystemExit):
        pc.main(["--only", "sim,teleport"])
    assert "unknown check(s): teleport" in capsys.readouterr().err


def test_main_reads_its_own_source(pc, monkeypatch):
    seen = {}
    monkeypatch.setattr(pc, "from_pc", lambda args, source: seen.setdefault("s", source) and 0)
    assert pc.main(["--only", "sim"]) == 0
    assert seen["s"] == SCRIPT.read_text()


def test_checks_table_is_complete(pc):
    assert len(set(pc.NAMES)) == len(pc.NAMES)
    for name, title, func, what in pc.CHECKS:
        assert func.__name__ == "check_" + ("time" if name == "time" else name)
        assert title and what
