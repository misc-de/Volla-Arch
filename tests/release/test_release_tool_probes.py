# The measuring tools that run on the phone, with what of them runs on the host:
#   tools/motion-probe.py  - records sensor-proxy-bridge's Motion/Gravity signals (D-Bus faked
#                            here) and splits a recording into movements (--analyze, real);
#   tools/face-flash.py    - the light patterns of liveness check A, a GTK 4 window (faked here,
#                            with a small main loop that runs its timers and frames);
#   tools/face-flash-analyze.py - recording + pattern log -> delay and brightness per pattern,
#                            on a synthetic recording whose face follows the patterns.

import json
import math
import random
import runpy
import subprocess
import sys
import types

import pytest

from conftest import TOOLS, load_script

MOTION = TOOLS / "motion-probe.py"
FLASH = TOOLS / "face-flash.py"
ANALYZE = TOOLS / "face-flash-analyze.py"


@pytest.fixture
def fake_gi(monkeypatch):
    """Installs namespaces in place of gi.repository.<name> for what a test imports."""
    gi = pytest.importorskip("gi")
    repository = pytest.importorskip("gi.repository")

    def install(**names):
        real = gi.require_version
        monkeypatch.setattr(gi, "require_version",
                            lambda ns, version: None if ns in names else real(ns, version))
        for n, obj in names.items():
            monkeypatch.setattr(repository, n, obj, raising=False)
            monkeypatch.setitem(sys.modules, "gi.repository." + n, obj)
    return install


# --- motion-probe: recording ----------------------------------------------------------------------

class Params:
    def __init__(self, *values):
        self.values = values

    def unpack(self):
        return self.values


def motion_fakes(events):
    """Gio/GLib for record(): the main loop delivers events [(µs, signal, (x, y, z))], then the
    timer that ends the recording."""
    state = {"now": 0, "subscribed": [], "calls": [], "timeout": None}

    class Bus:
        def signal_subscribe(self, *args):
            state["subscribed"].append(args)

        def call_sync(self, *args):
            state["calls"].append(args)

    class MainLoop:
        def run(self):
            on_signal = state["subscribed"][0][6]
            for t, signal, xyz in events:
                state["now"] = 5_000_000 + t
                on_signal(None, ":1.5", "/net/hadess/SensorProxy/Gravity",
                          "org.plinius.SensorProxyBridge.Gravity", signal, Params(*xyz))
            state["timeout"][1]()

        def quit(self):
            state["quit"] = True

    gio = types.SimpleNamespace(
        bus_get_sync=lambda bus_type, cancellable: state.setdefault("bus", Bus()),
        BusType=types.SimpleNamespace(SYSTEM="system"),
        DBusSignalFlags=types.SimpleNamespace(NONE=0), DBusCallFlags=types.SimpleNamespace(NONE=0))

    def timeout_add_seconds(seconds, fn):
        state["timeout"] = (seconds, fn)
    glib = types.SimpleNamespace(get_monotonic_time=lambda: state["now"], MainLoop=MainLoop,
                                 timeout_add_seconds=timeout_add_seconds)
    state["now"] = 5_000_000  # start of the recording
    return gio, glib, state


def test_record_claims_both_signals_and_writes_the_csv(fake_gi, monkeypatch, tmp_path):
    gio, glib, state = motion_fakes([(20_000, "Motion", (0.5, -0.25, 0.125)),
                                     (40_000, "Gravity", (0.0, 9.81, 0.1234))])
    fake_gi(Gio=gio, GLib=glib)
    out = tmp_path / "run.csv"
    monkeypatch.setattr(sys, "argv", [str(MOTION), str(out), "7"])
    runpy.run_path(str(MOTION), run_name="__main__")
    assert out.read_text() == "t,source,x,y,z\n0.020,m,0.500,-0.250,0.125\n0.040,g,0.000,9.810,0.123\n"
    assert state["subscribed"][0][:6] == (
        "net.hadess.SensorProxy", "org.plinius.SensorProxyBridge.Gravity", None,
        "/net/hadess/SensorProxy/Gravity", None, 0)
    assert [c[:4] for c in state["calls"]] == [
        ("net.hadess.SensorProxy", "/net/hadess/SensorProxy/Gravity",
         "org.plinius.SensorProxyBridge.Gravity", "ClaimMotion"),
        ("net.hadess.SensorProxy", "/net/hadess/SensorProxy/Gravity",
         "org.plinius.SensorProxyBridge.Gravity", "ClaimGravity")]
    assert state["timeout"][0] == 7 and state["quit"]


# --- motion-probe: analysis -----------------------------------------------------------------------

def recording(path):
    """33 Hz (so no gap is exactly REST_GAP): rest, a shake along x, rest, a slow push along y
    while the phone tilts by 90°, rest."""
    rows = ["t,source,x,y,z"]
    for k in range(234):
        t = k * 0.03
        if k <= 32:
            m = (0.1, 0, 0)                                   # below REST: no movement
        elif k <= 66:
            m = (2.0 if (k - 33) // 5 % 2 == 0 else -2.0, 0, 0)  # 34 samples, 7 half-swings
        elif 167 <= k <= 200:
            m = (0, 0.6, 0)                                   # above REST, too weak to swing
        else:
            m = (0, 0, 0)
        g = (0, 0, 9.81) if k < 200 else (0, 9.81, 0)
        rows.append(f"{t:.3f},m,{m[0]},{m[1]},{m[2]}")
        rows.append(f"{t:.3f},g,{g[0]},{g[1]},{g[2]}")
    path.write_text("\n".join(rows) + "\n")
    return path


def test_analyze_splits_movements_and_measures_them(tmp_path):
    r = subprocess.run([sys.executable, str(MOTION), "--analyze", str(recording(tmp_path / "r.csv"))],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "234 motion and 234 gravity samples over 7.0 s"
    # Shake: 0.99-2.97 s (one second of rest after it belongs to it), |a| 2 for 34 of 67 samples
    rms1 = math.sqrt(34 * 4 / 67)
    assert lines[1] == (f" 1:    1.0-   3.0 s ( 2.0 s)  peak   2.0  rms {rms1:4.1f}  energy    2.7"
                        "  swings  6 (x, 3.0/s)  tilt    0°")
    rms2 = math.sqrt(34 * 0.36 / 67)
    assert lines[2] == (f" 2:    5.0-   7.0 s ( 2.0 s)  peak   0.6  rms {rms2:4.1f}  energy    0.2"
                        "  swings  0 (y, 0.0/s)  tilt   90°")
    assert len(lines) == 3


def test_analyze_reads_what_record_wrote(fake_gi, monkeypatch, tmp_path, capsys):
    shake = [(i * 20_000, "Motion", (3.0 if i % 4 < 2 else -3.0, 0, 0)) for i in range(1, 21)]
    gravity = [(i * 20_000 + 10_000, "Gravity", (0, 0, 9.81)) for i in range(0, 21)]
    gio, glib, _ = motion_fakes(sorted(shake + gravity))
    fake_gi(Gio=gio, GLib=glib)
    probe = load_script(MOTION)
    out = tmp_path / "run.csv"
    probe.record(str(out), 1)
    probe.analyze(str(out))
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "20 motion and 21 gravity samples over 0.4 s"
    assert lines[1].startswith(" 1:    0.0-   0.4 s ( 0.4 s)  peak   3.0  rms  3.0")
    # Sign of x: + (sample 1), then pairs - + - ...: 10 changes in 0.38 s
    assert lines[1].endswith("energy    3.6  swings 10 (x, 26.3/s)  tilt    0°")


# --- face-flash ------------------------------------------------------------------------------------

PATTERN_NAMES = ["left", "right", "top", "bottom", "white", "black", "red", "green", "blue"]
W, H = 1080, 2400


class Cairo:
    def __init__(self):
        self.ops = []

    def set_source_rgb(self, *rgb):
        self.ops.append(("rgb", rgb))

    def paint(self):
        self.ops.append(("paint",))

    def rectangle(self, *rect):
        self.ops.append(("rect", rect))

    def fill(self):
        self.ops.append(("fill",))


def flash_fakes():
    """Gtk/GLib for face-flash.py: Application.run() is a main loop over the timers in ms; a
    queued draw paints one frame (tick callbacks with the frame time, then the draw function)."""
    loop = {"now": 0, "timers": [], "frames": [], "apps": []}

    class Clock:
        def __init__(self, us):
            self.us = us

        def get_frame_time(self):
            return self.us

    class DrawingArea:
        def __init__(self):
            self.ticks, self.dirty = [], False

        def set_draw_func(self, fn, data):
            self.draw = (fn, data)

        def add_tick_callback(self, fn):
            self.ticks.append(fn)

        def queue_draw(self):
            self.dirty = True

        def frame(self):
            for tick in self.ticks:
                tick(self, Clock(loop["now"] * 1000))
            cr = Cairo()
            self.draw[0](self, cr, W, H, self.draw[1])
            loop["frames"].append((loop["now"], cr.ops))
            self.dirty = False

    class ApplicationWindow:
        def __init__(self, application):
            self.application = application
            application.window = self

        def set_child(self, child):
            self.child = child

        def fullscreen(self):
            self.full = True

        def present(self):
            self.child.frame()  # the first frame, before any pattern

    class Application:
        def __init__(self, application_id):
            self.id, self.quitted, self.handlers = application_id, False, {}
            loop["apps"].append(self)

        def connect(self, signal, fn):
            self.handlers[signal] = fn

        def quit(self):
            self.quitted = True

        def run(self, argv):
            self.argv = argv
            self.handlers["activate"](self)
            while loop["timers"] and not self.quitted:
                loop["timers"].sort(key=lambda t: t[0])
                due, interval, fn, args = loop["timers"].pop(0)
                loop["now"] = due
                if fn(*args):
                    loop["timers"].append([due + interval, interval, fn, args])
                if self.window.child.dirty:
                    self.window.child.frame()
            return 0

    def timeout_add(ms, fn, *args):
        loop["timers"].append([loop["now"] + ms, ms, fn, args])

    gtk = types.SimpleNamespace(Application=Application, ApplicationWindow=ApplicationWindow,
                                DrawingArea=DrawingArea)
    glib = types.SimpleNamespace(SOURCE_CONTINUE=True, SOURCE_REMOVE=False, timeout_add=timeout_add)
    return gtk, glib, loop


def run_flash(fake_gi, monkeypatch, *args):
    gtk, glib, loop = flash_fakes()
    fake_gi(Gtk=gtk, GLib=glib)
    monkeypatch.setattr(sys, "argv", [str(FLASH), *args])
    with pytest.raises(SystemExit) as e:
        runpy.run_path(str(FLASH), run_name="__main__")
    assert e.value.code == 0
    return loop


def expected_sequence(seed, rounds):
    rng = random.Random(seed)
    seq = []
    for _ in range(rounds):
        names = list(PATTERN_NAMES)
        rng.shuffle(names)
        seq += names
    return seq


LIT = {"all": (0, 0, W, H), "left": (0, 0, W / 2, H), "right": (W / 2, 0, W / 2, H),
       "top": (0, 0, W, H / 2), "bottom": (0, H / 2, W, H / 2)}
LOOK = {"left": ("left", (1, 1, 1)), "right": ("right", (1, 1, 1)), "top": ("top", (1, 1, 1)),
        "bottom": ("bottom", (1, 1, 1)), "white": ("all", (1, 1, 1)), "black": (None, (0, 0, 0)),
        "red": ("all", (1, 0, 0)), "green": ("all", (0, 1, 0)), "blue": ("all", (0, 0, 1))}


def painted(name):
    """The cairo calls of one frame showing a pattern: black ground, then the lit part."""
    part, colour = LOOK[name]
    ops = [("rgb", (0, 0, 0)), ("paint",), ("rgb", colour)]
    return ops + ([("rect", LIT[part]), ("fill",)] if part else [])


def test_face_flash_shows_the_seeded_sequence_and_logs_paint_times(fake_gi, monkeypatch,
                                                                   tmp_path, capsys):
    log = tmp_path / "flash.json"
    loop = run_flash(fake_gi, monkeypatch, "--seed", "7", "--log", str(log))
    seq = expected_sequence(7, 2)
    app = loop["apps"][0]
    assert app.id == "org.plinius.FaceFlash" and app.quitted and app.window.full
    data = json.loads(log.read_text())
    assert data["seed"] == 7 and data["step_ms"] == 200
    # Black for 800 ms, then one pattern every 200 ms; the time is the frame's, in ns
    assert data["shown"] == [{"pattern": name, "time_ns": (800 + 200 * i) * 1_000_000}
                             for i, name in enumerate(seq)]
    frames = loop["frames"]
    assert frames[0] == (0, painted("black"))  # the window's first frame, before the sequence
    assert frames[1:] == [(800 + 200 * i, painted(name)) for i, name in enumerate(seq)]
    assert capsys.readouterr().out.strip() == f"seed 7, 18 patterns, log {log}"


def test_face_flash_rounds_step_and_a_random_seed(fake_gi, monkeypatch, tmp_path):
    log = tmp_path / "flash.json"
    loop = run_flash(fake_gi, monkeypatch, "--rounds", "1", "--step-ms", "50", "--log", str(log))
    data = json.loads(log.read_text())
    assert 0 <= data["seed"] < 1 << 31 and data["step_ms"] == 50
    # The logged seed reproduces the sequence
    seq = expected_sequence(data["seed"], 1)
    assert [s["pattern"] for s in data["shown"]] == seq
    assert [s["time_ns"] for s in data["shown"]] == [(800 + 50 * i) * 1_000_000 for i in range(9)]
    assert loop["apps"][0].quitted


def test_face_flash_closes_its_window_when_the_log_cannot_be_written(fake_gi, monkeypatch,
                                                                     tmp_path, capsys):
    log = tmp_path / "missing-dir" / "flash.json"
    loop = run_flash(fake_gi, monkeypatch, "--seed", "1", "--rounds", "1", "--log", str(log))
    assert loop["apps"][0].quitted and not log.exists()
    err = capsys.readouterr().err
    # errno, not its text: another test may have left the messages in the host's language
    assert err.startswith(f"cannot write {log}: ") and "[Errno 2]" in err


# --- face-flash-analyze ----------------------------------------------------------------------------

FW, FH = 96, 52
# Upright positions of the points the analysis reads (orientation 0: also the sensor positions)
SPOTS = {"cheek_r": (20, 24), "cheek_l": (50, 24), "forehead": (35, 10), "nose": (35, 24),
         "chin": (35, 40), "background": (78, 25)}
MESH = {"cheek_r": 50, "cheek_l": 280, "forehead": 151, "nose": 1, "chin": 152}
BASE = {"cheek_r": 50, "cheek_l": 50, "forehead": 50, "nose": 50, "chin": 50, "background": 40}
# Brightness over the black screen per pattern: a face in relief, lit from the screen
GAIN = {
    "white": dict(cheek_r=40, cheek_l=40, forehead=40, nose=40, chin=40, background=5),
    "left": dict(cheek_r=30, cheek_l=10, forehead=20, nose=20, chin=20, background=2),
    "right": dict(cheek_r=10, cheek_l=30, forehead=20, nose=20, chin=20, background=2),
    "top": dict(cheek_r=20, cheek_l=20, forehead=30, nose=20, chin=10, background=2),
    "bottom": dict(cheek_r=20, cheek_l=20, forehead=10, nose=20, chin=30, background=2),
    **{c: dict(cheek_r=15, cheek_l=15, forehead=15, nose=15, chin=15, background=1)
       for c in ("red", "green", "blue")},
}
T0 = 10_000_000_000
MS = 1_000_000


def truth(shown, t, step):
    if not shown or t < shown[0][1]:
        return "before"
    if t >= shown[-1][1] + step:
        return "after"
    return [name for name, start in shown if start <= t][-1]


def probe_dir(path, shown, delay_ms=100, step_ms=200, span=(-300, 2100), seed=42):
    """Frames every 10 ms (5 ms into each 10 ms slot), lit by what the screen showed delay_ms
    before; plus frames without a face, which must be skipped."""
    path.mkdir()
    shown_t = [(name, T0 + i * step_ms * MS) for i, name in enumerate(shown)]
    k = 0
    for ms in range(span[0] + 5, span[1], 10):
        t = T0 + ms * MS
        name = truth(shown_t, t - delay_ms * MS, step_ms * MS)
        luma = bytearray([0] * (FW * FH))
        for spot, (x, y) in SPOTS.items():
            value = BASE[spot] + GAIN.get(name, {}).get(spot, 0)
            for yy in range(y - 6, y + 7):
                luma[yy * FW + x - 6:yy * FW + x + 7] = bytes([value]) * 13
        (path / f"frame-{k:03d}.nv21").write_bytes(bytes(luma) + bytes(FW * FH // 2))
        mesh = "".join(f"mesh {i} {SPOTS[s][0]:.2f} {SPOTS[s][1]:.2f} 0.00\n"
                       for s, i in MESH.items())
        (path / f"frame-{k:03d}.txt").write_text(
            f"size {FW} {FH} orientation 0\ntime_ns {t}\nfound 1\nscore 0.9000\n"
            "box 10.0 4.0 60.0 46.0\npresence 0.9900\nkeypoint 0 1.0 2.0\n" + mesh +
            "embedding 0.1 0.2\n")
        k += 1
    for j in range(3):
        (path / f"frame-{k + j:03d}.txt").write_text(
            f"size {FW} {FH} orientation 0\ntime_ns {T0}\nfound 0\n")
    log = path.parent / (path.name + ".json")
    log.write_text(json.dumps({"seed": seed, "step_ms": step_ms, "shown": [
        {"pattern": name, "time_ns": start} for name, start in shown_t]}))
    return log


def analyze(directory, log):
    pytest.importorskip("numpy")
    r = subprocess.run([sys.executable, str(ANALYZE), str(directory), str(log)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def table(out):
    """{pattern: [n, 6 differences, left-right, top-bottom]} of the per-pattern table."""
    rows = {}
    for line in out.splitlines():
        part = line.split()
        if len(part) == 10 and part[0] in GAIN.keys() | {"before", "black", "after"}:
            rows[part[0]] = [int(part[1])] + [float(v) for v in part[2:]]
    return rows


SEQUENCE = ["black", "white", "left", "right", "top", "bottom", "red", "green", "blue"]


def test_analysis_finds_the_delay_and_the_relief(tmp_path):
    log = probe_dir(tmp_path / "probe", SEQUENCE)
    out = analyze(tmp_path / "probe", log)
    lines = out.splitlines()
    assert lines[0] == "240 frames with a face, 9 patterns of 200 ms (seed 42)"
    # lit frames: white 40, the four sides 20, the colours 15 above black on the face
    assert lines[1] == "delay screen -> camera about 100 ms (white/coloured minus black: 20.6 luma)"
    rows = table(out)
    assert rows["before"] == [40] + [0.0] * 8
    assert rows["black"] == [20] + [0.0] * 8
    assert rows["after"] == [20] + [0.0] * 8
    assert rows["white"] == [20, 40, 40, 40, 40, 40, 5, 0, 0]
    assert rows["left"] == [20, 30, 10, 20, 20, 20, 2, 20, 0]
    assert rows["right"] == [20, 10, 30, 20, 20, 20, 2, -20, 0]
    assert rows["top"] == [20, 20, 20, 30, 20, 10, 2, 0, 20]
    assert rows["bottom"] == [20, 20, 20, 10, 20, 30, 2, 0, -20]
    assert rows["red"] == [20, 15, 15, 15, 15, 15, 1, 0, 0]
    assert "face under white: +40.0 luma, background +5.0" in lines
    assert "direction left/right: +40.0 luma" in lines
    assert "direction top/bottom: +40.0 luma" in lines


def test_analysis_of_a_recording_that_ended_before_the_patterns(tmp_path):
    # The patterns start only after the last frame: nothing lit, no delay to find
    log = probe_dir(tmp_path / "probe", SEQUENCE, span=(-3000, -2500))
    out = analyze(tmp_path / "probe", log)
    assert "delay screen -> camera about 0 ms (white/coloured minus black: 0.0 luma)" in out
    assert list(table(out)) == ["before"]
    assert "face under white" not in out


def test_analysis_without_side_patterns(tmp_path):
    log = probe_dir(tmp_path / "probe", ["black", "white"], span=(-300, 700))
    out = analyze(tmp_path / "probe", log)
    assert "delay screen -> camera about 100 ms" in out
    assert "face under white: +40.0 luma, background +5.0" in out
    assert "direction" not in out


@pytest.fixture
def fa():
    pytest.importorskip("numpy")
    return load_script(ANALYZE)


def test_to_sensor_is_the_inverse_rotation_of_face_pipeline(fa):
    # As upright_to_sensor() in pkgbuilds/plinius-face/face-pipeline.c, for a 4x3 sensor frame
    frame = {"w": 4, "h": 3}
    for orientation, xy in {0: (1, 2), 90: (2, 1), 180: (2, 0), 270: (1, 1)}.items():
        assert fa.to_sensor(dict(frame, orientation=orientation), 1, 2) == xy


def test_measure_on_a_rotated_frame_takes_the_background_beside_the_face(fa):
    import numpy as np
    # Sensor 40x30, shown upright rotated by 90°: upright width 30. The face box sits right, so
    # the background is read left of it, at upright (10, 10) = sensor (10, 19)
    luma = np.zeros((30, 40), np.float32)
    luma[19 - 6:19 + 7, 10 - 6:10 + 7] = 77
    frame = {"w": 40, "h": 30, "orientation": 90, "luma": luma, "box": [20, 5, 28, 15],
             "mesh": {i: (25.0, 2.0) for i in MESH.values()}}
    values = fa.measure(frame)
    assert values["background"] == 77
    assert values["nose"] == 0
    # A point outside the frame has no pixels
    assert math.isnan(fa.mean_at(dict(frame, orientation=0), 500, 500))


def test_pattern_at(fa):
    shown = [{"pattern": "black", "time_ns": 100}, {"pattern": "white", "time_ns": 300}]
    assert [fa.pattern_at(shown, t, 200) for t in (50, 100, 299, 300, 499, 500)] == \
        ["before", "black", "black", "white", "white", "after"]
    assert fa.pattern_at([], 5, 200) == "before"


def test_face_flash_import_opens_no_window(fake_gi):
    gtk, glib, loop = flash_fakes()
    fake_gi(Gtk=gtk, GLib=glib)
    flash = load_script(FLASH)
    assert loop["apps"] == [] and loop["timers"] == []
    # Every pattern lights a known part of the screen
    assert {part for part, _ in flash.PATTERNS.values()} == {"left", "right", "top", "bottom",
                                                             "all", "none"}
