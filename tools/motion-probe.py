#!/usr/bin/env python3
# Probe: logs sensor-proxy-bridge's Motion (linear acceleration) and Gravity signals at 50 Hz
# into a CSV, for telling a shake from a pick-up or a tilt (plinius-ambient's snow globe).
#
# Usage on the phone: motion-probe.py OUT.csv SECONDS
# Columns: t (s since start), source (m = motion, g = gravity), x, y, z (m/s²).
# Afterwards: motion-probe.py --analyze OUT.csv splits the run at resting phases and prints
# peak, RMS and swing count of the linear acceleration per movement.
import math
import sys


def record(path, seconds):
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    start = GLib.get_monotonic_time()
    out = open(path, "w")
    out.write("t,source,x,y,z\n")

    def on_signal(conn, sender, obj, iface, signal, params):
        t = (GLib.get_monotonic_time() - start) / 1e6
        x, y, z = params.unpack()
        out.write(f"{t:.3f},{'m' if signal == 'Motion' else 'g'},{x:.3f},{y:.3f},{z:.3f}\n")

    bus.signal_subscribe("net.hadess.SensorProxy", "org.plinius.SensorProxyBridge.Gravity", None,
                         "/net/hadess/SensorProxy/Gravity", None, Gio.DBusSignalFlags.NONE, on_signal)
    for name in ("Motion", "Gravity"):
        bus.call_sync("net.hadess.SensorProxy", "/net/hadess/SensorProxy/Gravity",
                      "org.plinius.SensorProxyBridge.Gravity", "Claim" + name, None, None,
                      Gio.DBusCallFlags.NONE, 1000, None)
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(seconds, loop.quit)
    loop.run()
    out.close()


# Linear acceleration below this counts as rest (m/s²)
REST = 0.4
# A movement ends after this long at rest (s)
REST_GAP = 1.0


def analyze(path):
    motion, gravity = [], []
    for line in open(path).readlines()[1:]:
        t, src, x, y, z = line.strip().split(",")
        (motion if src == "m" else gravity).append((float(t), float(x), float(y), float(z)))
    print(f"{len(motion)} motion and {len(gravity)} gravity samples over {motion[-1][0]:.1f} s")

    # Movements: stretches where |a| exceeds REST, merged while the gaps are under REST_GAP
    events, current, last_active = [], None, -1e9
    for t, x, y, z in motion:
        a = math.sqrt(x * x + y * y + z * z)
        if a > REST:
            if current is None or t - last_active > REST_GAP:
                current = []
                events.append(current)
            last_active = t
        if current is not None and t - last_active <= REST_GAP:
            current.append((t, x, y, z, a))
    for n, ev in enumerate(events, 1):
        mags = [e[4] for e in ev]
        dur = ev[-1][0] - ev[0][0]
        rms = math.sqrt(sum(m * m for m in mags) / len(mags))
        # Swings: sign changes of the strongest axis, above REST either way
        axis = max(range(3), key=lambda i: sum(abs(e[1 + i]) for e in ev))
        swings, sign = 0, 0
        for e in ev:
            v = e[1 + axis]
            if abs(v) > 2 * REST:
                s = 1 if v > 0 else -1
                if sign and s != sign:
                    swings += 1
                sign = s
        # Energy: integral of |a|² over the movement (what the snow globe would sum up)
        energy = sum(m * m for m in mags) * 0.02
        # Tilt change over the movement from the gravity vector
        g0 = min(gravity, key=lambda g: abs(g[0] - ev[0][0]))
        g1 = min(gravity, key=lambda g: abs(g[0] - ev[-1][0]))
        dot = sum(g0[i] * g1[i] for i in (1, 2, 3))
        norm = math.dist((0, 0, 0), g0[1:]) * math.dist((0, 0, 0), g1[1:])
        tilt = math.degrees(math.acos(max(-1, min(1, dot / norm)))) if norm else 0
        print(f"{n:2}: {ev[0][0]:6.1f}-{ev[-1][0]:6.1f} s ({dur:4.1f} s)  peak {max(mags):5.1f}  "
              f"rms {rms:4.1f}  energy {energy:6.1f}  swings {swings:2} ({'xyz'[axis]}, "
              f"{swings / dur if dur else 0:3.1f}/s)  tilt {tilt:4.0f}°")


if __name__ == "__main__":
    if sys.argv[1] == "--analyze":
        analyze(sys.argv[2])
    else:
        record(sys.argv[1], int(sys.argv[2]))
