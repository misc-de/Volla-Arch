# The phone as plinius-usb sees it, for tests/daemons-hw/test_dhw_usb*.py: plinius-usb.c built
# into usb/dhw_usb_harness.c, with configfs, sysfs, /proc, /run and its settings as directories
# in one tmp directory, the programs it starts as stubs there, and (daemon mode) a private bus.

import queue
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from conftest import GCOV_DIR, FakeBin
from dhw_harness import PKG, build_harness, run, sandboxed_source

HERE = Path(__file__).resolve().parent
USB = PKG / "plinius-usb"
SRC = USB / "plinius-usb.c"

LAYOUT = ("cfg", "run", "var", "udc", "power_supply", "typec", "v4l", "dev", "proc", "etc", "bin",
          "lib")


def replacements(d):
    return [
        ('"/var/lib/plinius-usb/settings"', f'"{d}/var/settings"'),
        ('"/run/plinius-usb"', f'"{d}/run/plinius-usb"'),
        ('"/sys/kernel/config/usb_gadget/plinius"', f'"{d}/cfg/usb_gadget/plinius"'),
        ('"/sys/kernel/config/usb_gadget"', f'"{d}/cfg/usb_gadget"'),
        ('"/sys/kernel/config"', f'"{d}/cfg"'),
        ('"/sys/class/udc"', f'"{d}/udc"'),
        ('"/sys/class/power_supply"', f'"{d}/power_supply"'),
        ('"/sys/class/typec"', f'"{d}/typec"'),
        ('"/usr/bin/umtprd"', f'"{d}/bin/umtprd"'),
        ('"/run/systemd/network/40-plinius-usb.network.d"', f'"{d}/run/network/40-plinius-usb.network.d"'),
        ('"/proc/sys/net/ipv4/ip_forward"', f'"{d}/ip_forward"'),
        ('"/usr/bin/plinius-usb-webcam"', f'"{d}/bin/plinius-usb-webcam"'),
        ('"/usr/lib/gstreamer-1.0/libgstuvcgadget.so"', f'"{d}/lib/libgstuvcgadget.so"'),
        ('"/sys/class/video4linux"', f'"{d}/v4l"'),
        ('"/sys/usb/pogo_state"', f'"{d}/pogo_state"'),
        ('"/proc"', f'"{d}/proc"'),
        ('"/dev/%s"', f'"{d}/dev/%s"'),
        ('"/etc/machine-id"', f'"{d}/etc/machine-id"'),
        ('"/usr/bin/dnsmasq"', f'"{d}/bin/dnsmasq"'),
    ]


def build(d):
    """The harness for plinius-usb.c in d; its paths all point into d"""
    sandboxed_source(SRC, d, replacements(d))
    code = (HERE / "usb" / "dhw_usb_harness.c").read_text()
    return build_harness(d, "usb", code, flags=[f'-DDHW_ROOT="{d}"', f"-I{USB}", "-lpthread",
                                                 "-Wno-unused-parameter"],
                         pkgs=("gio-unix-2.0", "gudev-1.0"))


class Result:
    def __init__(self, r):
        self.returncode = r.returncode
        self.stdout = r.stdout
        self.stderr = r.stderr
        self.lines = r.stdout.splitlines()

    def of(self, command, nth=0):
        """The output lines of a command (its nth run)"""
        seen = -1
        out = None
        for line in self.lines:
            if line.startswith("> "):
                if out is not None:
                    return out
                if line[2:] == command:
                    seen += 1
                    if seen == nth:
                        out = []
                continue
            if out is not None:
                out.append(line)
        assert out is not None, f"{command!r} not in the output:\n{self.stdout}"
        return out

    def changed(self):
        """Every PropertiesChanged, in order: (property, value)"""
        return [tuple(line.split(" ", 2)[1:]) for line in self.lines if line.startswith("changed ")]


class Phone:
    """The directories of one test; files are written with put() and read with get()"""

    def __init__(self, d, exe):
        self.d = Path(d)
        self.exe = exe
        self.bin = FakeBin(self.d / "bin")

    def reset(self):
        for sub in LAYOUT:
            shutil.rmtree(self.d / sub, ignore_errors=True)
        for f in ("pogo_state", "ip_forward"):
            if (self.d / f).exists():
                (self.d / f).unlink()
        for sub in LAYOUT:
            (self.d / sub).mkdir()
        # main() makes its runtime directory
        (self.d / "run/plinius-usb").mkdir()
        self.bin = FakeBin(self.d / "bin")
        self.put("udc/11201000.usb/state", "not attached\n")
        self.put("udc/dummy_udc.0/state", "not attached\n")
        self.put("power_supply/battery/type", "Battery\n")
        self.put("power_supply/battery/capacity", "50\n")
        self.put("power_supply/usb/type", "USB\n")
        self.put("power_supply/usb/online", "0\n")
        self.put("power_supply/usb/usb_type", "[Unknown] SDP CDP DCP\n")
        self.put("etc/machine-id", "0123456789abcdef0123456789abcdef\n")
        self.put("lib/libgstuvcgadget.so", "")
        self.put("ip_forward", "0\n")
        # iptables-legacy: no rule there to delete, any other change works
        self.bin.stub("iptables-legacy", 'case " $* " in *" -D "*) exit 1 ;; esac\nexit 0')
        self.bin.stub("networkctl")
        self.bin.stub("umtprd", f'touch "{self.d}/run/plinius-usb/ffs-mtp/ep1"\nexec sleep 30 </dev/null >/dev/null 2>&1')
        self.bin.stub("dnsmasq", "exec sleep 30 </dev/null >/dev/null 2>&1")
        self.bin.stub("plinius-usb-webcam")
        return self

    def put(self, rel, text):
        p = self.d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def get(self, rel):
        p = self.d / rel
        return p.read_text() if p.exists() else None

    def computer(self, usb_type="SDP", online="1"):
        """Something on USB-C as charger detection sees it: SDP/CDP a computer, DCP a charger"""
        types = " ".join(f"[{t}]" if t == usb_type else t for t in ("Unknown", "SDP", "CDP", "DCP"))
        self.put("power_supply/usb/online", online + "\n")
        self.put("power_supply/usb/usb_type", types + "\n")

    def gadget(self, rel=""):
        return self.d / "cfg/usb_gadget/plinius" / rel

    def typec(self, role="sink", data="device", port_type="dual", pd="yes",
              mode="usb_power_delivery", partner=True):
        roles = "[source] sink" if role == "source" else "source [sink]"
        datas = "host [device]" if data == "device" else "[host] device"
        self.put("typec/port0/power_role", roles + "\n")
        self.put("typec/port0/data_role", datas + "\n")
        self.put("typec/port0/port_type", f"[{port_type}] source sink\n")
        self.put("typec/port0/power_operation_mode", mode + "\n")
        if partner:
            self.put("typec/port0-partner/supports_usb_power_delivery", pd + "\n")
        else:
            shutil.rmtree(self.d / "typec/port0-partner", ignore_errors=True)

    def webcam_node(self, name="video3"):
        """The video node binding the UVC function makes (a character device: /dev/null's)"""
        (self.d / "v4l" / name).symlink_to(
            f"../../devices/platform/soc/11201000.usb/gadget.0/video4linux/{name}")
        (self.d / "dev" / name).symlink_to("/dev/null")
        return self.d / "dev" / name

    def holder(self, pid, comm="plinius-usb-web", closes=False, stubborn=False):
        """A process with the webcam node open"""
        fd = self.d / "proc" / str(pid) / "fd"
        fd.mkdir(parents=True)
        (fd / "0").symlink_to(self.d / "etc/machine-id")
        (fd / "7").symlink_to("/dev/null")
        self.put(f"proc/{pid}/comm", comm + "\n")
        if closes:
            self.put(f"proc/{pid}/closes", "")
        if stubborn:
            self.put(f"proc/{pid}/stubborn", "")

    def env(self):
        return self.bin.env(isolated=True)

    def run(self, *commands, check=True):
        return Result(run(self.exe, *commands, env=self.env(), check=check, timeout=60))


# ---- Daemon mode: plinius-usb's own main() on a private bus ----

BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>{listen}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


class PrivateBus:
    """A dbus-daemon of the test alone: no service files, nothing of the host's buses"""

    def __init__(self, tmp):
        tmp = Path(tmp)
        sock = tmp / "bus"
        # A socket path has at most 107 bytes; a long tmp path gets an abstract socket instead
        listen = (f"unix:path={sock}" if len(str(sock)) < 100
                  else f"unix:abstract=dhw-usb-{uuid.uuid4().hex}")
        conf = tmp / "bus.conf"
        conf.write_text(BUS_CONFIG.format(listen=listen))
        self.proc = subprocess.Popen(["dbus-daemon", f"--config-file={conf}", "--nofork",
                                      "--print-address"], stdout=subprocess.PIPE, text=True)
        self.address = self.proc.stdout.readline().strip()
        assert self.address, "dbus-daemon did not start"

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)


class Daemon:
    """A harness running in daemon mode; commands go to its stdin, its output is collected"""

    def __init__(self, phone, bus_address, args=("daemon",), cwd=None, session_bus=None):
        env = phone.env()
        env.update(ASAN_OPTIONS="detect_leaks=0:abort_on_error=0:exitcode=86",
                   UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=1:exitcode=87",
                   G_DEBUG="", LC_ALL="C", DBUS_SYSTEM_BUS_ADDRESS=bus_address)
        # Never the host's session bus (nor an X11 autolaunch of one)
        env["DBUS_SESSION_BUS_ADDRESS"] = session_bus or bus_address
        env.pop("DISPLAY", None)
        self.slow = 5 if GCOV_DIR else 1
        self.proc = subprocess.Popen([str(phone.exe), *args], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     env=env, cwd=cwd)
        self.lines = []
        self._q = queue.Queue()
        self._err = []
        threading.Thread(target=self._read, args=(self.proc.stdout, self._q), daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()

    def _read(self, stream, q):
        for line in stream:
            q.put(line.rstrip("\n"))
        q.put(None)

    def _drain(self):
        for line in self.proc.stderr:
            self._err.append(line)

    @property
    def stderr(self):
        return "".join(self._err)

    def wait_line(self, predicate, timeout=20):
        end = time.monotonic() + timeout * self.slow
        while time.monotonic() < end:
            try:
                line = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                break
            self.lines.append(line)
            if predicate(line):
                return line
        raise AssertionError("no such line; output:\n" + "\n".join(self.lines) + "\n" + self.stderr)

    def command(self, text):
        """Runs a command in the daemon's main loop; returns its output lines"""
        start = len(self.lines)
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()
        self.wait_line(lambda line: line == "done")
        out = self.lines[start:-1]
        # What the daemon printed by itself meanwhile comes before the echo
        return out[out.index(f"> {text}") + 1:]

    def finish(self, timeout=20):
        """Waits for the end; returns the exit code"""
        try:
            if not any(line.startswith("exit ") for line in self.lines):
                self.wait_line(lambda line: line.startswith("exit "), timeout)
        finally:
            try:
                self.proc.stdin.close()
            except BrokenPipeError:
                pass
            rc = self.proc.wait(timeout=timeout * self.slow)
        return rc

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


__all__ = ["Phone", "PrivateBus", "Daemon", "Result", "build", "USB", "SRC"]
