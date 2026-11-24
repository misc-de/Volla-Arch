#!/usr/bin/env python3
# Evaluates a recording for liveness check A of plinius-face: frames of plinius-face-probe -s
# (NV21, landmarks, arrival time) against the light patterns tools/face-flash.py logged.
#
# Per frame: mean luma at points of the face (cheeks, forehead, nose, chin, from Face Mesh) and
# of the background. Finds the delay screen → camera that best explains the face's brightness by
# the patterns, then shows per pattern how bright each point got against the black screen, and the
# left/right and top/bottom contrasts a real face should show (its relief shades) and a flat photo
# or a screen should not.
#
# Usage: face-flash-analyze.py <probe dir> <flash log.json>   (numpy needed)
import json
import os
import sys

import numpy as np

# Face Mesh landmarks (MediaPipe canonical face): subject's right cheek is image left
POINTS = {"cheek_r": 50, "cheek_l": 280, "forehead": 151, "nose": 1, "chin": 152}
RADIUS = 6  # pixels around each point


def load_frames(directory):
    frames = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".txt"):
            continue
        info = {"mesh": {}}
        with open(os.path.join(directory, name)) as f:
            for line in f:
                part = line.split()
                if part[0] == "size":
                    info["w"], info["h"], info["orientation"] = int(part[1]), int(part[2]), int(part[4])
                elif part[0] == "time_ns":
                    info["time_ns"] = int(part[1])
                elif part[0] == "found":
                    info["found"] = int(part[1])
                elif part[0] == "box":
                    info["box"] = [float(v) for v in part[1:5]]
                elif part[0] == "mesh":
                    info["mesh"][int(part[1])] = (float(part[2]), float(part[3]))
        if info.get("found", 0) <= 0:
            continue
        luma = np.fromfile(os.path.join(directory, name[:-4] + ".nv21"), np.uint8)
        info["luma"] = luma[: info["w"] * info["h"]].reshape(info["h"], info["w"]).astype(np.float32)
        frames.append(info)
    return frames


def to_sensor(frame, u, v):
    """Upright frame position -> sensor position, as upright_to_sensor() in face-pipeline.c"""
    w1, h1 = frame["w"] - 1, frame["h"] - 1
    return {90: (v, h1 - u), 180: (w1 - u, h1 - v), 270: (w1 - v, u)}.get(frame["orientation"], (u, v))


def mean_at(frame, u, v):
    x, y = to_sensor(frame, u, v)
    x, y = int(round(x)), int(round(y))
    patch = frame["luma"][max(0, y - RADIUS) : y + RADIUS + 1, max(0, x - RADIUS) : x + RADIUS + 1]
    return float(patch.mean()) if patch.size else float("nan")


def measure(frame):
    values = {k: mean_at(frame, *frame["mesh"][i]) for k, i in POINTS.items()}
    # Background: beside the face box, at eye height, on the side with more room
    x0, y0, x1, y1 = frame["box"]
    width = frame["h"] if frame["orientation"] in (90, 270) else frame["w"]
    bx = x0 / 2 if x0 > width - x1 else (x1 + width) / 2
    values["background"] = mean_at(frame, bx, (y0 + y1) / 2)
    return values


def pattern_at(shown, time_ns, step_ns):
    """What the screen showed: "before" the sequence (black lead-in), "after" it (window gone)"""
    if not shown or time_ns < shown[0]["time_ns"]:
        return "before"
    if time_ns >= shown[-1]["time_ns"] + step_ns:
        return "after"
    current = shown[0]["pattern"]
    for entry in shown:
        if entry["time_ns"] <= time_ns:
            current = entry["pattern"]
        else:
            break
    return current


def main():
    directory, log_path = sys.argv[1:3]
    with open(log_path) as f:
        log = json.load(f)
    shown = log["shown"]
    step_ns = log["step_ms"] * 1_000_000
    frames = load_frames(directory)
    print(f"{len(frames)} frames with a face, {len(shown)} patterns of {log['step_ms']} ms (seed {log['seed']})")
    for frame in frames:
        frame["values"] = measure(frame)

    # Delay: the one where the face is brightest under white and darkest under black
    face = np.array([np.nanmean([frame["values"][k] for k in POINTS]) for frame in frames])
    best = None
    for delay_ms in range(0, 401, 10):
        names = [pattern_at(shown, fr["time_ns"] - delay_ms * 1_000_000, step_ns) for fr in frames]
        lit = np.array([n not in ("black", "before", "after") for n in names])
        dark = np.array([n in ("black", "before") for n in names])
        if not lit.any() or not dark.any():
            continue
        score = face[lit].mean() - face[dark].mean()
        if best is None or score > best[1]:
            best = (delay_ms, score)
    delay = best[0] if best else 0
    print(f"delay screen -> camera about {delay} ms (white/coloured minus black: {best[1] if best else 0:.1f} luma)")

    groups = {}
    for frame in frames:
        name = pattern_at(shown, frame["time_ns"] - delay * 1_000_000, step_ns)
        groups.setdefault(name, []).append(frame["values"])
    # Reference: the black screen, in the sequence and in the lead-in
    dark = groups.get("black", []) + groups.get("before", [])
    black = {k: np.nanmean([v[k] for v in dark]) for k in list(POINTS) + ["background"]}

    print("\nmean luma per pattern, as the difference to the black screen:")
    keys = list(POINTS) + ["background"]
    print(f"{'pattern':8} {'n':>3} " + " ".join(f"{k:>10}" for k in keys) + "   left-right  top-bottom")
    rows = {}
    for name in ["before", "black", "white", "left", "right", "top", "bottom", "red", "green", "blue", "after"]:
        if name not in groups:
            continue
        means = {k: np.nanmean([v[k] for v in groups[name]]) for k in keys}
        diff = {k: means[k] - black[k] for k in keys}
        lr = diff["cheek_r"] - diff["cheek_l"]  # image left minus image right
        tb = diff["forehead"] - diff["chin"]
        rows[name] = (diff, lr, tb)
        print(f"{name:8} {len(groups[name]):3d} " + " ".join(f"{diff[k]:10.1f}" for k in keys) + f"   {lr:10.1f}  {tb:10.1f}")
    if "white" in rows:
        face_gain = np.nanmean([rows["white"][0][k] for k in POINTS])
        print(f"\nface under white: {face_gain:+.1f} luma, background {rows['white'][0]['background']:+.1f}")
        if "left" in rows and "right" in rows:
            print(f"direction left/right: {rows['left'][1] - rows['right'][1]:+.1f} luma")
        if "top" in rows and "bottom" in rows:
            print(f"direction top/bottom: {rows['top'][2] - rows['bottom'][2]:+.1f} luma")
    print("\nA real face should get brighter on the side the light comes from (left/right, top/bottom"
          "\npatterns with opposite signs), a photo far less, a phone screen showing a photo hardly at all.")


if __name__ == "__main__":
    main()
