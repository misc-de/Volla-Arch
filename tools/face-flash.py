#!/usr/bin/env python3
# Measuring tool for liveness check A of plinius-face (docs/HARDWARE.md, Gesichtserkennung): shows
# a random sequence of light patterns full screen, the screen as the light source on the face,
# and logs when each pattern was really painted (GTK frame clock, CLOCK_MONOTONIC like the
# camera's frame times). Runs on the phone in the user's session, while plinius-face-probe -s
# records the front camera; tools/face-flash-analyze.py puts both together.
#
# Usage (on the phone, as the session user): face-flash.py [--seed N] [--step-ms 200] [--rounds 2]
#     [--log file]
import argparse
import json
import random
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

# Pattern: name and the parts of the screen that are lit, with their colour
PATTERNS = {
    "left": ("left", (1, 1, 1)),
    "right": ("right", (1, 1, 1)),
    "top": ("top", (1, 1, 1)),
    "bottom": ("bottom", (1, 1, 1)),
    "white": ("all", (1, 1, 1)),
    "black": ("none", (0, 0, 0)),
    "red": ("all", (1, 0, 0)),
    "green": ("all", (0, 1, 0)),
    "blue": ("all", (0, 0, 1)),
}
LEAD_IN_MS = 800  # black first, so the camera settles on the dark screen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--step-ms", type=int, default=200)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--log", default="/tmp/face-flash.json")
    args = parser.parse_args()

    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1 << 31)
    rng = random.Random(seed)
    sequence = []
    for _ in range(args.rounds):
        names = list(PATTERNS)
        rng.shuffle(names)
        sequence += names

    app = Gtk.Application(application_id="org.plinius.FaceFlash")
    state = {"index": -1, "shown": [], "pending": None}

    def draw(area, cr, width, height, data):
        name = sequence[state["index"]] if 0 <= state["index"] < len(sequence) else "black"
        part, colour = PATTERNS[name]
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        cr.set_source_rgb(*colour)
        rects = {
            "all": (0, 0, width, height),
            "left": (0, 0, width / 2, height),
            "right": (width / 2, 0, width / 2, height),
            "top": (0, 0, width, height / 2),
            "bottom": (0, height / 2, width, height / 2),
        }
        if part in rects:
            cr.rectangle(*rects[part])
            cr.fill()

    def on_tick(widget, clock):
        # The frame time is when this frame is expected on the screen, in µs of CLOCK_MONOTONIC
        if state["pending"] is not None:
            state["shown"].append({"pattern": state["pending"], "time_ns": clock.get_frame_time() * 1000})
            state["pending"] = None
        return GLib.SOURCE_CONTINUE

    def step(area):
        state["index"] += 1
        if state["index"] >= len(sequence):
            # Always close the full-screen window, also when the log cannot be written
            try:
                with open(args.log, "w") as f:
                    json.dump({"seed": seed, "step_ms": args.step_ms, "shown": state["shown"]}, f, indent=1)
                print(f"seed {seed}, {len(state['shown'])} patterns, log {args.log}")
            except OSError as error:
                print(f"cannot write {args.log}: {error}", file=sys.stderr)
            finally:
                app.quit()
            return GLib.SOURCE_REMOVE
        state["pending"] = sequence[state["index"]]
        area.queue_draw()
        return GLib.SOURCE_CONTINUE

    def activate(app):
        window = Gtk.ApplicationWindow(application=app)
        area = Gtk.DrawingArea()
        area.set_draw_func(draw, None)
        area.add_tick_callback(on_tick)
        window.set_child(area)
        window.fullscreen()
        window.present()

        def start():
            step(area)
            # The timer may drift; what counts is the logged paint time of each pattern
            GLib.timeout_add(args.step_ms, step, area)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(LEAD_IN_MS, start)

    app.connect("activate", activate)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
