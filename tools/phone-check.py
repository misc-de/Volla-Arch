#!/usr/bin/env python3
"""Checks the phone's everyday functions live, from the PC over SSH.

plinius-health (the self-test after boot) only asks whether each layer is there: the container,
a modem in ModemManager, wlan0, hci0. This tool asks whether the functions work: is the SIM
registered on a network, can calls ring and SMS go out, does mobile data reach the internet, does
a Wi-Fi and a Bluetooth scan see anything, does the GPS receiver hear satellites, do the sensors,
cameras, audio nodes, battery, fingerprint reader, NFC, torch and vibration motor answer, do
OpenGL ES, gl4es and Vulkan reach the GPU (drawn off screen, nothing appears).

It changes nothing and reaches nobody: no call, no SMS, no pairing or connection, no sound, no
vibration, no light, no camera frame (only the camera portal gives apps frames, on purpose: the
tool does not go past it), no microphone recording (plinius-appaccess would ask on the screen).
Radios the user switched off stay off and their checks are skipped. A Wi-Fi and a Bluetooth scan
and a GPS session are started (--no-scan leaves the scans out); two small HTTP requests go to
NetworkManager's connectivity address, one over mobile data, one over Wi-Fi.

Usage: tools/phone-check.py [--host USER@HOST] [--only NAME,...] [--skip NAME,...]
                            [--gps-seconds N] [--no-scan] [--json] [--list]
  --host         the phone, as root (default: $PHONE or root@10.15.19.82, USB networking);
                 $PHONE_SSH_OPTS replaces the SSH options (default: the USB address's host key)
  --gps-seconds  how long to listen for satellites and a fix (default 25)
  --json         one JSON object per check instead of the table
  --list         the checks and what they look at
The script sends itself to the phone (ssh ... python3 -I - --on-phone) and runs there as root;
on the phone itself: python3 phone-check.py --on-phone.
Exit status: 0 nothing failed (warnings allowed), 2 at least one check failed, 1 the tool could
not run (phone not reachable, not root).
"""
import argparse
import concurrent.futures
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time

# Places on the phone (the tests point them into a temporary directory)
SYS = "/sys"
PROC = "/proc"
RUN = "/run"
USR = "/usr"
ETC = "/etc"
HOME = "/home"
NMEA_SOCKET = "/run/gnss-nmea-bridge/nmea.sock"
CONNECTIVITY_URI = "http://ping.archlinux.org/nm-check.txt"  # NetworkManager's, if none is set

DEFAULT_PHONE = "root@10.15.19.82"
DEFAULT_SSH_OPTS = "-o HostKeyAlias=10.15.19.82 -o ConnectTimeout=8 -o BatchMode=yes"

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


class Phone:
    """Runs commands on the phone, as root or as the user of the graphical session."""

    def __init__(self):
        self.user = session_user(self)
        self.uid = None
        if self.user:
            rc, out = self.run(["id", "-u", self.user])
            self.uid = out.strip() if rc == 0 else None
        self._modems = None

    def run(self, cmd, timeout=15, user=False):
        """(exit status, stdout) of cmd; 127 if the program is missing, 124 on a timeout."""
        if user:
            if not self.user or not self.uid:
                return 1, ""
            rundir = f"{RUN}/user/{self.uid}"
            cmd = ["runuser", "-u", self.user, "--", "env", f"XDG_RUNTIME_DIR={rundir}",
                   f"DBUS_SESSION_BUS_ADDRESS=unix:path={rundir}/bus", *cmd]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                               errors="replace")
        except FileNotFoundError:
            return 127, ""
        except subprocess.TimeoutExpired as e:
            out = e.stdout or ""
            return 124, out.decode(errors="replace") if isinstance(out, bytes) else out
        return p.returncode, p.stdout

    def busctl(self, *args, user=False):
        """The data of a busctl call/get-property as Python values, None if it failed."""
        rc, out = self.run(["busctl", "--user" if user else "--system", "--json=short", *args],
                           user=user)
        if rc != 0:
            return None
        try:
            data = json.loads(out)["data"]
        except (ValueError, KeyError, TypeError):
            return None
        return data

    def ofono(self, path, iface, method="GetProperties"):
        """The properties of an oFono interface as {name: value}, {} if it cannot be read."""
        data = self.busctl("call", "org.ofono", path, iface, method)
        if not data:
            return {}
        return {k: v.get("data") for k, v in data[0].items()}

    def modems(self):
        """oFono's modems: [(path, properties)], each property a plain value."""
        if self._modems is None:
            data = self.busctl("call", "org.ofono", "/", "org.ofono.Manager", "GetModems")
            self._modems = [(path, {k: v.get("data") for k, v in props.items()})
                            for path, props in (data[0] if data else [])]
        return self._modems

    def bus_names(self, user=False):
        data = self.busctl("call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                           "org.freedesktop.DBus", "ListNames", user=user)
        return set(data[0]) if data else set()


def session_user(phone):
    """The user of the Wayland session on seat0, as plinius-health finds it ('' if none)."""
    rc, out = phone.run(["loginctl", "list-sessions", "--no-legend"])
    for line in out.splitlines() if rc == 0 else []:
        sid = line.split()[0] if line.split() else ""
        props = {}
        for p in ("Seat", "Type", "Name"):
            rc, val = phone.run(["loginctl", "show-session", sid, "-p", p, "--value"])
            props[p] = val.strip()
        if props["Seat"] == "seat0" and props["Type"] == "wayland":
            return props["Name"]
    return ""


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


# ---------------------------------------------------------------------------------------------
# The checks. Each returns (status, message).

def check_selftest(phone):
    rc, out = phone.run(["plinius-health", "status"])
    if rc == 127:
        return SKIP, "plinius-health not installed"
    if out.strip() == "not checked yet":
        return SKIP, "self-test after boot has not run yet"
    failed = [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("fail ")]
    if failed:
        return FAIL, "; ".join(failed)
    return OK, f"{sum(1 for line in out.splitlines() if line.startswith('ok '))} layers ok"


def sim_modems(phone):
    """The modems with a SIM in them: [(path, properties, SimManager properties)]."""
    found = []
    for path, props in phone.modems():
        if "org.ofono.SimManager" not in props.get("Interfaces", []):
            continue
        sim = phone.ofono(path, "org.ofono.SimManager")
        if sim.get("Present"):
            found.append((path, props, sim))
    return found


def active_modems(phone):
    """The modems with a SIM that are online; (list, reason to skip if empty)."""
    sims = sim_modems(phone)
    if not sims:
        return [], "no SIM"
    online = [(p, props) for p, props, sim in sims if props.get("Online")]
    if not online:
        return [], "modem offline (flight mode?)"
    return online, ""


def check_sim(phone):
    if not phone.modems():
        return FAIL, "oFono has no modem (ofono, Android container)"
    sims = sim_modems(phone)
    if not sims:
        return FAIL, "no SIM card found"
    locked = [f"{p}: {sim.get('PinRequired')}" for p, _, sim in sims
              if sim.get("PinRequired", "none") != "none"]
    if locked:
        return FAIL, "SIM locked: " + ", ".join(locked)
    online = [p for p, props, _ in sims if props.get("Online")]
    if not online:
        return SKIP, f"{len(sims)} SIM(s), all modems offline (flight mode?)"
    return OK, f"{len(sims)} SIM(s) ready, online: {', '.join(online)}"


def check_network(phone):
    modems, why = active_modems(phone)
    if not modems:
        return SKIP, why
    parts, bad = [], []
    for path, _ in modems:
        reg = phone.ofono(path, "org.ofono.NetworkRegistration")
        status = reg.get("Status", "unknown")
        if status in ("registered", "roaming"):
            parts.append(f"{path}: {reg.get('Name', '?')}, {reg.get('Technology', '?')}, "
                         f"signal {reg.get('Strength', '?')} %" +
                         (", roaming" if status == "roaming" else ""))
        else:
            bad.append(f"{path}: {status}")
    if bad:
        return FAIL, "not registered: " + "; ".join(bad + parts)
    return OK, "; ".join(parts)


def check_calls(phone):
    modems, why = active_modems(phone)
    if not modems:
        return SKIP, why
    notes, problems = [], []
    for path, props in modems:
        if "org.ofono.VoiceCallManager" not in props.get("Interfaces", []):
            problems.append(f"{path}: no voice calls (VoiceCallManager missing)")
            continue
        numbers = phone.ofono(path, "org.ofono.VoiceCallManager").get("EmergencyNumbers", [])
        if "112" not in numbers:
            problems.append(f"{path}: 112 not among the emergency numbers")
        ims = phone.ofono(path, "org.ofono.IpMultimediaSystem")
        tech = phone.ofono(path, "org.ofono.NetworkRegistration").get("Technology", "")
        if ims.get("Registered") and ims.get("VoiceCapable"):
            notes.append(f"{path}: VoLTE")
        elif tech in ("lte", "nr"):
            notes.append(f"{path}: no VoLTE on {tech}, calls drop to 2G/3G")
    if not phone.user:
        problems.append("no graphical session: nothing would ring")
    else:
        rc, _ = phone.run(["pgrep", "-u", phone.user, "-x", "gnome-calls"])
        if rc != 0:
            problems.append("GNOME Calls not running: incoming calls would not ring")
        if "org.mobian_project.CallAudio" not in phone.bus_names(user=True):
            problems.append("callaudiod missing: no audio routing in calls")
    if problems:
        return FAIL, "; ".join(problems + notes)
    if any("no VoLTE" in n for n in notes):
        return WARN, "; ".join(notes)
    return OK, "; ".join(["ready to ring and dial", *notes])


def check_sms(phone):
    modems, why = active_modems(phone)
    if not modems:
        return SKIP, why
    problems = []
    for path, props in modems:
        if "org.ofono.MessageManager" not in props.get("Interfaces", []):
            problems.append(f"{path}: no SMS (MessageManager missing)")
        elif not phone.ofono(path, "org.ofono.MessageManager").get("ServiceCenterAddress"):
            problems.append(f"{path}: no SMS service centre, SMS cannot be sent")
    if not phone.user:
        problems.append("no graphical session: SMS would not be shown")
    elif "sm.puri.Chatty" not in phone.bus_names(user=True):
        problems.append("Chatty not running: incoming SMS would not be shown")
    if problems:
        return FAIL, "; ".join(problems)
    return OK, "service centre set, Chatty running"


def nm_devices(phone):
    """NetworkManager's devices: [(device, type, state, connection)]."""
    rc, out = phone.run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"])
    devices = []
    for line in out.splitlines() if rc == 0 else []:
        # nmcli -t escapes ':' inside a field as '\:'
        fields = [f.replace("\\:", ":") for f in re.split(r"(?<!\\):", line)]
        if len(fields) >= 4:
            devices.append(tuple(fields[:4]))
    return devices


def connectivity_uri(phone):
    rc, out = phone.run(["NetworkManager", "--print-config"])
    section = ""
    for line in out.splitlines() if rc == 0 else []:
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[connectivity]" and line.startswith("uri="):
            return line[4:]
    return CONNECTIVITY_URI


def reaches_internet(phone, iface):
    """(True, seconds) if an HTTP request through iface gets an answer, (False, reason)."""
    rc, out = phone.run(["curl", "-s", "-o", "/dev/null", "-m", "10", "-w",
                         "%{http_code} %{time_total}", "--interface", iface,
                         connectivity_uri(phone)], timeout=15)
    if rc == 127:
        return False, "curl missing"
    code, _, secs = out.strip().partition(" ")
    if rc == 0 and code == "200":
        return True, f"{float(secs):.1f} s"
    return False, f"no answer through {iface} (curl {rc}, HTTP {code or '-'})"


def ip_iface(phone, device):
    rc, out = phone.run(["nmcli", "-g", "GENERAL.IP-IFACE", "device", "show", device])
    return out.strip() if rc == 0 and out.strip() else device


def check_data(phone):
    modems, why = active_modems(phone)
    if not modems:
        return SKIP, why
    gsm = [d for d in nm_devices(phone) if d[1] == "gsm"]
    if not gsm:
        return FAIL, "NetworkManager has no mobile broadband device (ModemManager, ofono2mm)"
    connected = [d for d in gsm if d[2].startswith("connected")]
    if not connected:
        rc, out = phone.run(["nmcli", "-t", "-f", "TYPE,AUTOCONNECT", "connection", "show"])
        if any(line == "gsm:yes" for line in out.splitlines()):
            return FAIL, "mobile data is on but not connected"
        return SKIP, "mobile data switched off"
    iface = ip_iface(phone, connected[0][0])
    ok, info = reaches_internet(phone, iface)
    if ok:
        return OK, f"internet through {iface} ({connected[0][3]}), answer in {info}"
    return FAIL, info


def check_wifi(phone, scan=True):
    rc, out = phone.run(["nmcli", "-t", "radio", "wifi"])
    if out.strip() == "disabled":
        return SKIP, "Wi-Fi switched off"
    wifi = [d for d in nm_devices(phone) if d[1] == "wifi" and d[0] == "wlan0"]
    if not wifi:
        return FAIL, "no Wi-Fi device wlan0 in NetworkManager"
    parts, status = [], OK
    if scan:
        rc, out = phone.run(["nmcli", "-t", "-f", "BSSID", "device", "wifi", "list",
                             "ifname", "wlan0", "--rescan", "yes"], timeout=30)
        seen = len({line for line in out.splitlines() if line.strip()}) if rc == 0 else 0
        if rc != 0:
            return FAIL, f"scan failed (nmcli {rc})"
        parts.append(f"scan sees {seen} access point(s)")
        if seen == 0:
            status = WARN
    if wifi[0][2].startswith("connected"):
        ok, info = reaches_internet(phone, "wlan0")
        if ok:
            parts.append(f"connected, internet in {info}")
        else:
            parts.append(f"connected, but {info}")
            status = WARN  # a hotspot without internet or a captive portal is no phone fault
    else:
        parts.append("not connected")
    return status, ", ".join(parts)


DEVICE_RE = re.compile(r"Device ((?:[0-9A-F]{2}:){5}[0-9A-F]{2})")


def check_bluetooth(phone, scan=True):
    rc, out = phone.run(["bluetoothctl", "show"])
    if rc == 127:
        return FAIL, "bluetoothctl missing"
    if "Controller" not in out:
        return FAIL, "BlueZ has no adapter (bluebinder)"
    if "Powered: yes" not in out:
        return SKIP, "Bluetooth switched off"
    rc, out = phone.run(["bluetoothctl", "devices", "Connected"])
    connected = len(DEVICE_RE.findall(out)) if rc == 0 else 0
    parts = [f"{connected} device(s) connected"] if connected else []
    if not scan:
        return OK, ", ".join(parts + ["adapter on"])
    rc, out = phone.run(["bluetoothctl", "--timeout", "8", "scan", "on"], timeout=15)
    seen = len(set(DEVICE_RE.findall(out)))
    parts.insert(0, f"scan sees {seen} device(s)")
    return (OK if seen else WARN), ", ".join(parts)


def gsv_satellites(line, sats):
    """Adds the satellites of a GSV sentence to sats: {(system, prn): snr}."""
    body = line.split("*", 1)[0].split(",")
    talker = body[0][1:3]
    for i in range(4, len(body) - 3, 4):
        prn, snr = body[i], body[i + 3]
        if prn:
            sats[(talker, prn)] = max(sats.get((talker, prn), 0), int(snr) if snr.isdigit() else 0)


def gga_fix(line):
    """Number of satellites used if a GGA sentence carries a fix, else None."""
    f = line.split("*", 1)[0].split(",")
    if len(f) > 7 and f[6] not in ("", "0"):
        return int(f[7]) if f[7].isdigit() else 0
    return None


def check_gps(phone, seconds=25):
    if not os.path.exists(NMEA_SOCKET):
        return FAIL, "no NMEA socket (gnss-nmea-bridge)"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    start = time.monotonic()
    sats, sentences, used, buf = {}, 0, None, b""
    try:
        sock.connect(NMEA_SOCKET)
        while used is None:
            left = start + seconds - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(left)
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("$"):
                    continue
                sentences += 1
                if line[3:6] == "GSV":
                    gsv_satellites(line, sats)
                elif line[3:6] == "GGA":
                    used = gga_fix(line)
                    if used is not None:
                        break
    except OSError as e:
        return FAIL, f"NMEA socket: {e.strerror or e}"
    finally:
        sock.close()
    took = time.monotonic() - start
    heard = sorted((snr for snr in sats.values() if snr > 0), reverse=True)
    if used is not None:
        return OK, f"fix after {took:.0f} s with {used} satellites"
    if heard:
        return OK, (f"{len(heard)} satellite(s) heard (best {heard[0]} dB-Hz), no fix in "
                    f"{seconds} s (indoors?)")
    if sentences:
        # Without signals the receiver still lists the satellites it expects above (almanac)
        return WARN, (f"receiver runs, {len(sats)} satellite(s) expected in view, none heard in "
                      f"{seconds} s (indoors?)")
    return FAIL, f"no NMEA sentence in {seconds} s (GNSS HAL, Android container)"


def check_sensors(phone):
    rc, out = phone.run(["timeout", "4", "monitor-sensor", "--accel", "--light", "--proximity"],
                        timeout=10)
    if rc == 127:
        return FAIL, "monitor-sensor missing (iio-sensor-proxy)"
    found, missing = [], []
    light = re.search(r"Has ambient light sensor \(value: ([0-9.]+), unit: (\w+)", out)
    for what, pattern in (("accelerometer", "Has accelerometer"),
                          ("light sensor", "Has ambient light sensor"),
                          ("proximity sensor", "Has proximity sensor")):
        (found if pattern in out else missing).append(what)
    if missing:
        return FAIL, "missing: " + ", ".join(missing) + " (sensor-proxy-bridge)"
    if not light:
        return OK, ", ".join(found)
    return OK, f"{', '.join(found)}; light {float(light.group(1)):.0f} {light.group(2)}"


def pw_nodes(phone):
    """The session's PipeWire nodes: {node.name: (media.class, state)}."""
    rc, out = phone.run(["pw-dump"], user=True)
    try:
        objects = json.loads(out) if rc == 0 else []
    except ValueError:
        objects = []
    nodes = {}
    for o in objects:
        if o.get("type") == "PipeWire:Interface:Node":
            info = o.get("info") or {}
            props = info.get("props") or {}
            nodes[props.get("node.name", "")] = (props.get("media.class", ""), info.get("state"))
    return nodes


def check_camera(phone):
    if not phone.user:
        return SKIP, "no graphical session"
    rc, out = phone.run(["systemctl", "--user", "is-active", "camera-hal-bridge"], user=True)
    if out.strip() != "active":
        return FAIL, f"camera-hal-bridge is {out.strip() or 'not running'}"
    if "org.plinius.CameraBridge" not in phone.bus_names(user=True):
        return FAIL, "camera-hal-bridge runs but is not on the session bus"
    rc, out = phone.run(["lxc-attach", "-n", "android", "--", "/system/bin/dumpsys",
                         "media.camera"], timeout=20)
    m = re.search(r"Number of camera devices: (\d+)", out)
    if not m or not int(m.group(1)):
        return FAIL, "CameraService in the Android container reports no camera"
    hal = int(m.group(1))
    # WirePlumber gives native clients no permissions on the camera nodes
    # (find-plinius-camera-access.lua), so pw-dump sees them only before the permissions are set:
    # counted when seen, not missed when hidden.
    nodes = {n: s for n, s in pw_nodes(phone).items() if n.startswith("hal_camera_")}
    broken = [n for n, (_, state) in nodes.items() if state == "error"]
    if broken:
        return FAIL, "camera node(s) in error: " + ", ".join(sorted(broken))
    if nodes and len(nodes) != hal:
        return FAIL, f"CameraService has {hal} camera(s), PipeWire {len(nodes)} node(s)"
    in_use = len(re.findall(r"Device \d+ is open", out))
    return OK, f"{hal} cameras in CameraService, bridge running" + \
        (f", {in_use} in use" if in_use else "")


def check_audio(phone):
    if not phone.user:
        return SKIP, "no graphical session"
    nodes = pw_nodes(phone)
    want = {"hal_output": "speaker", "hal_earpiece": "earpiece", "hal_input": "microphone"}
    missing = [w for n, w in want.items() if n not in nodes]
    broken = [w for n, w in want.items() if n in nodes and nodes[n][1] == "error"]
    if missing or broken:
        return FAIL, "; ".join(filter(None, [
            missing and "missing: " + ", ".join(missing) + " (audio-hal-bridge)",
            broken and "in error: " + ", ".join(broken)]))
    return OK, "speaker, earpiece and microphone in PipeWire"


def check_display(phone):
    problems, parts = [], []
    devices = read(f"{PROC}/bus/input/devices", "")
    if 'N: Name="fts_ts"' not in devices:
        problems.append("no touchscreen (fts_ts)")
    level = read(f"{SYS}/class/leds/lcd-backlight/brightness")
    top = read(f"{SYS}/class/leds/lcd-backlight/max_brightness")
    if level is None or top is None:
        problems.append("no backlight (lcd-backlight)")
    else:
        parts.append(f"backlight {int(level)}/{int(top)}" if int(level) else "screen off")
    if problems:
        return FAIL, "; ".join(problems)
    return OK, "touchscreen, " + ", ".join(parts)


# Run as the session user in a python3 of its own: a context on a 16x16 pbuffer (nothing on the
# screen), cleared to a known colour and read back. One line "PROBE {json}" on stdout; the
# driver's own chatter around it is ignored.
GL_PROBE = r'''
import ctypes as C, json, sys

def probe(egl_path, gl_path, api):
    try:
        egl, gl = C.CDLL(egl_path), C.CDLL(gl_path)
    except OSError as e:
        return {"error": f"cannot load: {e}"}
    vp, i = C.c_void_p, C.c_int
    for name, res, args in (
            ("eglGetDisplay", vp, [vp]), ("eglInitialize", i, [vp, vp, vp]),
            ("eglBindAPI", i, [i]), ("eglChooseConfig", i, [vp, vp, vp, i, vp]),
            ("eglCreatePbufferSurface", vp, [vp, vp, vp]),
            ("eglCreateContext", vp, [vp, vp, vp, vp]), ("eglMakeCurrent", i, [vp, vp, vp, vp]),
            ("eglGetError", i, []), ("eglTerminate", i, [vp])):
        f = getattr(egl, name)
        f.restype, f.argtypes = res, args
    gl.glGetString.restype, gl.glGetString.argtypes = C.c_char_p, [i]
    gl.glClearColor.argtypes = [C.c_float] * 4
    gl.glClear.argtypes = [i]
    gl.glReadPixels.argtypes = [i, i, i, i, i, i, vp]

    def failed(step):
        return {"error": f"{step} failed (EGL error 0x{egl.eglGetError() & 0xffff:04x})"}

    d = egl.eglGetDisplay(None)
    major, minor = i(), i()
    if not d or not egl.eglInitialize(d, C.byref(major), C.byref(minor)):
        return failed("eglInitialize")
    try:
        if api == "gl" and not egl.eglBindAPI(0x30A2):  # EGL_OPENGL_API
            return failed("eglBindAPI(OpenGL)")
        # SURFACE_TYPE pbuffer, RENDERABLE_TYPE ES3 or OpenGL, 8 bits red and blue
        attrs = (i * 9)(0x3033, 0x1, 0x3040, 0x40 if api == "es" else 0x8, 0x3024, 8, 0x3022, 8,
                        0x3038)
        cfg, n = vp(), i()
        if not egl.eglChooseConfig(d, attrs, C.byref(cfg), 1, C.byref(n)) or n.value < 1:
            return failed("eglChooseConfig")
        surf = egl.eglCreatePbufferSurface(d, cfg, (i * 5)(0x3057, 16, 0x3056, 16, 0x3038))
        if not surf:
            return failed("eglCreatePbufferSurface")
        ctx_attrs = (i * 3)(0x3098, 3, 0x3038) if api == "es" else (i * 1)(0x3038)
        ctx = egl.eglCreateContext(d, cfg, None, ctx_attrs)
        if not ctx:
            return failed("eglCreateContext")
        if not egl.eglMakeCurrent(d, surf, surf, ctx):
            return failed("eglMakeCurrent")
        gl.glClearColor(0.0, 0.5, 1.0, 1.0)
        gl.glClear(0x4000)  # GL_COLOR_BUFFER_BIT
        pixel = (C.c_ubyte * 4)()
        gl.glReadPixels(0, 0, 1, 1, 0x1908, 0x1401, pixel)  # GL_RGBA, GL_UNSIGNED_BYTE
        text = {k: (gl.glGetString(v) or b"").decode(errors="replace")
                for k, v in (("vendor", 0x1F00), ("renderer", 0x1F01), ("version", 0x1F02))}
        egl.eglMakeCurrent(d, None, None, None)
        return {**text, "pixel": list(pixel)}
    finally:
        egl.eglTerminate(d)

print("PROBE " + json.dumps(probe(*sys.argv[1:4])), flush=True)
'''

SOFTWARE_RENDERERS = ("llvmpipe", "softpipe", "swrast", "lavapipe")


def expand_env(value, env):
    """$VAR, ${VAR}, ${VAR:-default} and ${VAR:+alternate} as systemd's environment.d has them."""
    def one(m):
        name = m.group(1) or m.group(2)
        cur = env.get(name, "")
        if m.group(3) == ":-":
            return cur or m.group(4)
        if m.group(3) == ":+":
            return m.group(4) if cur else ""
        return cur
    return re.sub(r"\$(?:([A-Za-z_]\w*)|\{([A-Za-z_]\w*)(?:(:[-+])([^}]*))?\})", one, value)


def session_env():
    """KEY=VALUE of systemd's environment.d, as the session's programs get them (libhybris, the
    XDG_DATA_DIRS in which the Vulkan loader looks for its drivers)."""
    env = {}
    for d in (f"{USR}/lib/environment.d", f"{ETC}/environment.d"):
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith(".conf"))
        except OSError:
            continue
        for name in names:
            for line in (read(os.path.join(d, name), "") or "").splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#") and re.fullmatch(r"[A-Za-z_]\w*", key):
                    env[key] = expand_env(value, env)
    return [f"{k}={v}" for k, v in env.items()]


def gl_probe(phone, egl, gl, api, extra=()):
    """The GL_PROBE answer as a dict; {"error": ...} if it gave none."""
    rc, out = phone.run([*session_env(), *extra, "python3", "-I", "-c", GL_PROBE, egl, gl, api],
                        timeout=30, user=True)
    for line in reversed(out.splitlines()):
        if line.startswith("PROBE "):
            try:
                return json.loads(line[6:])
            except ValueError:
                break
    if rc == 124:
        return {"error": "no answer within 30 s"}
    return {"error": f"probe ended with status {rc} and no answer"}


def gl_problem(what, result):
    """What is wrong with a GL_PROBE answer, or None."""
    if "error" in result:
        return f"{what}: {result['error']}"
    if any(s in result.get("renderer", "").lower() for s in SOFTWARE_RENDERERS):
        return f"{what} renders in software ({result['renderer']})"
    r, g, b, a = (result.get("pixel") or [0, 0, 0, 0])[:4]
    if not (r <= 8 and 120 <= g <= 136 and b >= 247 and a >= 247):
        return f"{what} draws a wrong colour ({r}, {g}, {b}, {a} instead of 0, 128, 255, 255)"
    return None


def check_opengl(phone):
    if not phone.user:
        return SKIP, "no graphical session"
    lib = f"{USR}/lib"
    es = gl_probe(phone, f"{lib}/libEGL.so.1", f"{lib}/libGLESv2.so.2", "es")
    problems = [gl_problem("OpenGL ES", es)]
    gl4es_dir = f"{lib}/gl4es"
    gl4es = None
    if os.path.exists(f"{gl4es_dir}/libGL.so.1"):
        # as /usr/bin/gl4es starts a program: its libGL and EGL wrapper first, drawing through
        # the real EGL and GLES
        gl4es = gl_probe(phone, f"{gl4es_dir}/libEGL.so.1", f"{gl4es_dir}/libGL.so.1", "gl",
                         [f"LD_LIBRARY_PATH={gl4es_dir}", f"LIBGL_EGL={lib}/libEGL.so.1",
                          f"LIBGL_GLES={lib}/libGLESv2.so.2", "LIBGL_NOBANNER=1"])
        problems.append(gl_problem("OpenGL through gl4es", gl4es))
    problems = [p for p in problems if p]
    if problems:
        return FAIL, "; ".join(problems)
    version = " ".join(es["version"].split()[:3])  # "OpenGL ES 3.2", without the build
    msg = f"{version} on {es['renderer']}"
    if gl4es:
        msg += f", OpenGL {gl4es['version'].split()[0]} through gl4es"
    return OK, msg + ", drawn and read back"


VK_PATH_VARS = ("VK_DRIVER_FILES", "VK_ICD_FILENAMES", "VK_ADD_DRIVER_FILES")


def stale_vulkan_launchers():
    """Desktop launchers whose Exec= names a Vulkan driver manifest that is not there."""
    dirs = [f"{USR}/share/applications", f"{USR}/local/share/applications"]
    try:
        dirs += sorted(os.path.join(HOME, u, ".local/share/applications")
                       for u in os.listdir(HOME))
    except OSError:
        pass
    stale = []
    for d in dirs:
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith(".desktop"))
        except OSError:
            continue
        for name in names:
            for line in (read(os.path.join(d, name), "") or "").splitlines():
                if not line.startswith("Exec="):
                    continue
                for m in re.finditer(r"\b(?:%s)=(\S+)" % "|".join(VK_PATH_VARS), line):
                    missing = [p for p in m.group(1).split(":") if p and not os.path.exists(p)]
                    if missing:
                        stale.append(f"{name} ({', '.join(missing)})")
    return stale


def vulkan_manifests():
    """(manifest, library) of every installed Vulkan driver; library None if it is missing."""
    found = []
    for d in (f"{ETC}/vulkan/icd.d", f"{USR}/share/vulkan/icd.d"):
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
        except OSError:
            continue
        for name in names:
            try:
                path = json.loads(read(os.path.join(d, name), ""))["ICD"]["library_path"]
            except (ValueError, KeyError, TypeError):
                found.append((name, None))
                continue
            full = path if path.startswith("/") else f"{USR}/lib/{path}"
            found.append((name, path if os.path.exists(full) else None))
    return found


def check_vulkan(phone):
    stale = stale_vulkan_launchers()
    if stale:
        return FAIL, "launcher(s) point at a missing Vulkan driver: " + "; ".join(stale)
    manifests = vulkan_manifests()
    if not manifests:
        return SKIP, "no Vulkan driver installed (hybris-vulkan)"
    broken = [name for name, lib in manifests if lib is None]
    if broken:
        return FAIL, "driver manifest without its library: " + ", ".join(broken)
    if not phone.user:
        return SKIP, "no graphical session"
    rc, out = phone.run([*session_env(), "vulkaninfo", "--summary"], timeout=30, user=True)
    if rc == 127:
        return WARN, f"{len(manifests)} driver(s) installed, device not checked " \
                     "(vulkaninfo missing: vulkan-tools)"
    devices = []
    for block in re.split(r"^GPU\d+:\s*$", out, flags=re.M)[1:]:
        fields = dict(re.findall(r"^\s+(\w+)\s+=\s+(.*?)\s*$", block, re.M))
        if "deviceName" in fields:
            devices.append(fields)
    if rc != 0 or not devices:
        return FAIL, "the Vulkan loader finds no device" + (f" (status {rc})" if rc else "")
    gpus = [f for f in devices if not f.get("deviceType", "").endswith("_CPU")]
    if not gpus:
        return FAIL, "only a software device: " + ", ".join(f["deviceName"] for f in devices)
    gpu = gpus[0]
    return OK, f"Vulkan {gpu.get('apiVersion', '?')} on {gpu['deviceName']}"


def check_battery(phone):
    rc, out = phone.run(["upower", "-i", "/org/freedesktop/UPower/devices/battery_battery"])
    info = dict(re.findall(r"^\s*([\w-]+):\s+(.*?)\s*$", out, re.M)) if rc == 0 else {}
    if info.get("present") != "yes":
        return FAIL, "UPower sees no battery"
    pct = float(info.get("percentage", "0").rstrip("%").replace(",", "."))
    msg = f"{pct:.0f} %, {info.get('state', 'unknown')}"
    return (WARN if pct < 15 and info.get("state") != "charging" else OK), msg


def check_fingerprint(phone):
    devices = phone.busctl("call", "net.reactivated.Fprint", "/net/reactivated/Fprint/Manager",
                           "net.reactivated.Fprint.Manager", "GetDevices")
    if not devices or not devices[0]:
        return FAIL, "fprintd has no reader (biomd)"
    if not phone.user:
        return OK, "reader present"
    rc, out = phone.run(["fprintd-list", phone.user])
    fingers = len(re.findall(r"^ - #\d+:", out, re.M))
    return OK, f"reader present, {fingers} finger(s) enrolled"


def check_face(phone):
    if "org.plinius.Face" not in phone.bus_names():
        return FAIL, "face unlock service missing (org.plinius.Face)"
    return OK, "face unlock service running"


def check_nfc(phone):
    powered = phone.busctl("get-property", "org.sailfishos.nfc.daemon", "/nfc0",
                           "org.neard.Adapter", "Powered")
    if powered is None:
        return FAIL, "no NFC adapter (nfcd)"
    return OK, "adapter present, " + ("powered" if powered else "not powered")


def check_torch(phone):
    top = read(f"{SYS}/class/leds/white:torch/max_brightness")
    if not top or not int(top):
        return FAIL, "no torch LED (white:torch)"
    rc, out = phone.run(["systemctl", "is-active", "plinius-torch"])
    if out.strip() != "active":
        return FAIL, f"plinius-torch is {out.strip() or 'not running'}"
    return OK, f"torch LED with {int(top)} levels"


def check_vibration(phone):
    if not os.path.isdir(f"{SYS}/class/leds/vibrator"):
        return FAIL, "no vibration motor (leds/vibrator)"
    if not phone.user:
        return OK, "motor present"
    if "org.sigxcpu.Feedback" not in phone.bus_names(user=True):
        return FAIL, "feedbackd not running: no vibration or notification sounds"
    return OK, "motor present, feedbackd running"


def check_time(phone):
    rc, out = phone.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if out.strip() == "yes":
        return OK, "clock synchronised"
    return WARN, "clock not synchronised (no network time yet?)"


def check_storage(phone):
    parts, status = [], OK
    for mount in ("/", "/home"):
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        free = st.f_bavail * st.f_frsize
        pct = 100 * st.f_bavail / st.f_blocks if st.f_blocks else 0
        parts.append(f"{mount} {free / 2**30:.1f} GiB free ({pct:.0f} %)")
        if pct < 2:
            status = FAIL
        elif pct < 5 and status == OK:
            status = WARN
    return status, ", ".join(parts)


# name, title, function, what it looks at
CHECKS = [
    ("selftest", "Self-test", check_selftest, "plinius-health: units, container, GPU"),
    ("sim", "SIM", check_sim, "SIM present and unlocked, modem online"),
    ("network", "Mobile network", check_network, "registered, operator, technology, signal"),
    ("calls", "Calls", check_calls, "voice calls, 112, VoLTE, GNOME Calls, callaudiod"),
    ("sms", "SMS", check_sms, "service centre, Chatty"),
    ("data", "Mobile data", check_data, "connected, HTTP request through the modem"),
    ("wifi", "Wi-Fi", check_wifi, "scan, HTTP request through wlan0 if connected"),
    ("bluetooth", "Bluetooth", check_bluetooth, "adapter, scan"),
    ("gps", "GPS", check_gps, "satellites heard, fix"),
    ("sensors", "Sensors", check_sensors, "accelerometer, light, proximity"),
    ("camera", "Cameras", check_camera, "bridge, CameraService in the container"),
    ("audio", "Audio", check_audio, "speaker, earpiece, microphone nodes"),
    ("display", "Display", check_display, "touchscreen, backlight"),
    ("opengl", "OpenGL", check_opengl, "OpenGL ES and gl4es draw off screen on the GPU"),
    ("vulkan", "Vulkan", check_vulkan, "driver, GPU device, launchers' driver paths"),
    ("battery", "Battery", check_battery, "charge, state"),
    ("fingerprint", "Fingerprint", check_fingerprint, "reader, enrolled fingers"),
    ("face", "Face unlock", check_face, "plinius-face service"),
    ("nfc", "NFC", check_nfc, "adapter"),
    ("torch", "Torch", check_torch, "LED, plinius-torch"),
    ("vibration", "Vibration", check_vibration, "motor, feedbackd"),
    ("time", "Clock", check_time, "network time"),
    ("storage", "Storage", check_storage, "free space on / and /home"),
]
NAMES = [c[0] for c in CHECKS]


def run_checks(phone, names, scan=True, gps_seconds=25, emit=print):
    """Runs the checks side by side; emits one JSON line per check, in the order of CHECKS."""
    extra = {"wifi": {"scan": scan}, "bluetooth": {"scan": scan},
             "gps": {"seconds": gps_seconds}}
    chosen = [c for c in CHECKS if c[0] in names]
    results = []

    def one(check):
        name, title, func, _ = check
        start = time.monotonic()
        try:
            status, message = func(phone, **extra.get(name, {}))
        except Exception as e:  # a check that breaks must not hide the others
            status, message = FAIL, f"check broke: {type(e).__name__}: {e}"
        return {"name": name, "title": title, "status": status, "message": message,
                "seconds": round(time.monotonic() - start, 1)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(chosen) or 1) as pool:
        for future in [pool.submit(one, c) for c in chosen]:
            result = future.result()
            results.append(result)
            emit(json.dumps(result), flush=True)
    return results


def on_phone(args):
    if os.geteuid() != 0:
        print("phone-check: must run as root on the phone", file=sys.stderr)
        return 1
    phone = Phone()
    results = run_checks(phone, args.names, scan=not args.no_scan,
                         gps_seconds=args.gps_seconds)
    return 2 if any(r["status"] == FAIL for r in results) else 0


COLORS = {OK: "32", WARN: "33", FAIL: "31", SKIP: "90"}


def render(result, color):
    label = result["status"].upper().ljust(4)
    if color:
        label = f"\033[{COLORS[result['status']]}m{label}\033[0m"
    return f"{label}  {result['title']:<15} {result['message']}"


def from_pc(args, source):
    host = args.host or os.environ.get("PHONE") or DEFAULT_PHONE
    opts = shlex.split(os.environ.get("PHONE_SSH_OPTS", DEFAULT_SSH_OPTS))
    remote = ["python3", "-I", "-", "--on-phone", "--only", ",".join(args.names),
              "--gps-seconds", str(args.gps_seconds)] + (["--no-scan"] if args.no_scan else [])
    cmd = ["ssh", *opts, host, shlex.join(remote)]
    color = sys.stdout.isatty() and not args.json
    results = []
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          text=True) as p:
        p.stdin.write(source)
        p.stdin.close()
        for line in p.stdout:
            try:
                result = json.loads(line)
            except ValueError:
                print(line, end="", file=sys.stderr)
                continue
            results.append(result)
            print(line.strip() if args.json else render(result, color), flush=True)
    if p.returncode == 255 or (p.returncode and not results):
        print(f"phone-check: no answer from {host} (ssh {p.returncode})", file=sys.stderr)
        return 1
    if not args.json:
        counts = {s: sum(1 for r in results if r["status"] == s) for s in (OK, WARN, FAIL, SKIP)}
        print(f"\n{counts[OK]} ok, {counts[WARN]} warnings, {counts[FAIL]} failed, "
              f"{counts[SKIP]} skipped")
    return 2 if any(r["status"] == FAIL for r in results) else 0


def names_list(value):
    names = [n for n in value.split(",") if n]
    unknown = [n for n in names if n not in NAMES]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown check(s): {', '.join(unknown)} "
                                         f"(known: {', '.join(NAMES)})")
    return names


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Checks the phone's everyday functions live.")
    ap.add_argument("--host")
    ap.add_argument("--only", type=names_list)
    ap.add_argument("--skip", type=names_list, default=[])
    ap.add_argument("--gps-seconds", type=int, default=25)
    ap.add_argument("--no-scan", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--on-phone", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    args.names = [n for n in (args.only or NAMES) if n not in args.skip]
    return args


def main(argv=None, source=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.list:
        for name, title, _, what in CHECKS:
            print(f"{name:<12} {title:<15} {what}")
        return 0
    if args.on_phone:
        return on_phone(args)
    if source is None:
        with open(os.path.abspath(__file__)) as f:
            source = f.read()
    return from_pc(args, source)


if __name__ == "__main__":
    sys.exit(main())
