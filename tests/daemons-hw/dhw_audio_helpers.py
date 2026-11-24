# Helpers for the audio-hal-bridge tests (test_dhw_audio*.py): one build of the harness
# (c/dhw_audio_harness.c) per session, runs of it in a test's own directory, and a private
# D-Bus session bus (its own dbus-daemon, socket in tmp, no activation) for the bridge's main.

import shutil
import signal
import struct
import subprocess
from pathlib import Path

import pytest

from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "audio-hal-bridge" / "audio-hal-bridge.c"
HARNESS = Path(__file__).resolve().parent / "c" / "dhw_audio_harness.c"

# AUDIO_DEVICE_* and the bridge's SINK_* (fakes/hardware/audio.h, audio-hal-bridge.c)
SPEAKER, EARPIECE, HEADSET, HEADPHONE, SCO = 0x2, 0x1, 0x4, 0x8, 0x10
MIC, SCO_MIC, HEADSET_MIC, FM = 0x80000004, 0x80000008, 0x80000010, 0x80002000
S_SPEAKER, S_EARPIECE, S_PHONES = 1, 2, 4
STEREO_IN, MONO_IN = 0xC, 0x10
# linux/input-event-codes.h
EV_SYN, SYN_REPORT, EV_SW = 0, 0, 5
SW_HEADPHONE_INSERT, SW_MICROPHONE_INSERT, SW_LINEOUT_INSERT = 2, 4, 6

_built = {}


def _cflags(*pkgs):
    r = subprocess.run(["pkg-config", "--cflags", *pkgs], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"missing on the host: {' '.join(pkgs)}")
    return r.stdout.split()


def harness(tmp_path_factory):
    """The harness binary. libpipewire and ALSA are not linked: their headers only, the
    functions are the harness's stand-ins."""
    if "exe" not in _built:
        d = tmp_path_factory.mktemp("audio")
        # The jack's input devices and the FM tuner: relative, in the directory a test runs in
        sandboxed_source(SRC, d, [('"/dev/input', '"input'), ('"/dev/fm"', '"fm"')])
        _built["exe"] = build_harness(d, "audio", HARNESS.read_text(),
                                      flags=[f"-I{SRC.parent}", *_cflags("libpipewire-0.3", "alsa")],
                                      pkgs=("gio-unix-2.0",))
    return _built["exe"]


def environment(cwd, **extra):
    env = {"XDG_STATE_HOME": str(cwd / "state"), "HOME": str(cwd), "PATH": "/usr/bin:/bin",
           # Never a real bus: the session bus is the test's own or none
           "DBUS_SESSION_BUS_ADDRESS": f"unix:path={cwd}/no-session-bus",
           "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={cwd}/no-system-bus",
           "GIO_USE_VFS": "local", "GSETTINGS_BACKEND": "memory"}
    env.update({k: str(v) for k, v in extra.items()})
    return env


class Audio:
    """Runs the harness in one test directory."""

    def __init__(self, exe, cwd):
        self.exe, self.cwd = exe, cwd

    def __call__(self, *cmds, check=True, timeout=20, **env):
        r = run(self.exe, *cmds, env=environment(self.cwd, **env), check=check, timeout=timeout,
                cwd=self.cwd)
        return r if not check else r.stdout

    def lines(self, *cmds, **env):
        return self(*cmds, **env).splitlines()

    def main(self, script, *pre, bus=None, check=True, timeout=30, **env):
        """The bridge's main: pre commands first, the script in the stand-in PipeWire thread."""
        path = self.cwd / "script"
        path.write_text("\n".join(script) + "\n")
        extra = dict(env)
        extra["FAKE_SCRIPT"] = path
        if bus:
            extra["DBUS_SESSION_BUS_ADDRESS"] = bus
        return run(self.exe, "main", *pre, env=environment(self.cwd, **extra), check=check,
                   timeout=timeout, cwd=self.cwd)


def jack_device(cwd, name, number=0, states=(), events=0):
    """An input device of the jack: its name, the switch states EVIOCGSW reports one after the
    other, and events SYN_REPORTs in the device (each makes the bridge read the state again)."""
    d = cwd / "input"
    d.mkdir(exist_ok=True)
    dev = d / f"event{number}"
    record = b"".join(struct.pack("llHHi", 0, 0, EV_SW, SW_HEADPHONE_INSERT, 1) +
                      struct.pack("llHHi", 0, 0, EV_SYN, SYN_REPORT, 0) for _ in range(events))
    dev.write_bytes(record)
    (d / f"event{number}.name").write_text(name + "\n")
    (d / f"event{number}.sw").write_text("\n".join(",".join(map(str, s)) or "-" for s in states) + "\n")
    return dev


class Bus:
    """A dbus-daemon of the test's own: its socket in tmp, nothing activatable."""

    def __init__(self, d):
        d.mkdir(parents=True, exist_ok=True)
        self.socket = d / "bus"
        conf = d / "bus.conf"
        conf.write_text(f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={self.socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
""")
        if shutil.which("dbus-daemon") is None:
            pytest.skip("no dbus-daemon")
        self.proc = subprocess.Popen(["dbus-daemon", f"--config-file={conf}", "--nofork", "--print-address"],
                                     stdout=subprocess.PIPE, text=True,
                                     env={"PATH": "/usr/bin:/bin", "HOME": str(d)})
        self.address = self.proc.stdout.readline().strip()
        assert self.address.startswith("unix:"), "dbus-daemon gave no address"

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        self.proc.wait(timeout=10)


@pytest.fixture
def audio(tmp_path_factory, tmp_path):
    """The harness, run in the test's own directory."""
    return Audio(harness(tmp_path_factory), tmp_path)


__all__ = ["Audio", "Bus", "SRC", "audio", "harness", "jack_device", "kv"]
