#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""plinius-udfps: the touch target for a fingerprint sensor under the display.

An under-display sensor cannot tell where the finger is, and an optical one needs the display
to light the finger. Android's UI does both; this does it for biomd: while biomd enrolls or
identifies, a circle sits over the sensor on top of everything (lock screen included).

A finger on it turns the circle white and reports PointerDown. Only the sensor is to light up,
yet the light comes from the backlight, which lights the whole panel. So, as Android does, the
rest of the screen gets a black veil first, exactly as dark as the backlight is about to get
brighter, with a glow and a scan beam around the sensor; then the backlight goes to full and
UiReady follows once the light is on screen. Lifting reports PointerUp, puts the brightness back
and then takes the veil away.

Runs in the Phosh session (XDG autostart). biomd can come up after the session (it waits for the
Android container) and restart with it, so this waits for biomd and follows it; while biomd
reports no sensor under the display it does nothing. Only draws in portrait: the sensor location
is given for the unrotated display.
"""

import glob
import math
import sys

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, Gio, GLib, Gtk, GtkLayerShell  # noqa: E402

BIOMD = "io.FuriOS.Biomd"
FP_PATH = "/io/FuriOS/Biomd/Fingerprint"
FP_IFACE = "io.FuriOS.Biomd.Fingerprint"

# biomd's State property
STATE_IDLE, STATE_ENROLLING, STATE_IDENTIFYING = 0, 1, 2
# FingerprintSensorType of the AIDL HAL
UNDER_DISPLAY_TYPES = (2, 3)  # ultrasonic, optical

BACKLIGHT_DIR = "/sys/class/backlight"
# The veil is on screen this long after it was painted before the backlight goes up, so the
# rest of the screen never flashes bright
BOOST_DELAY_MS = 20
# The panel needs a moment to show the white circle at full brightness before the HAL captures
UI_READY_DELAY_MS = 60

# The panel's light grows about with the backlight level to this power (white measured through
# the light sensor at levels 30, 130 and 230: 1.8 to 2.0)
PANEL_GAMMA = 1.9
# Pixel values are gamma encoded: black at alpha a over them leaves (1 - a) ** 2.2 of the light
DISPLAY_GAMMA = 2.2

ACCENT = (0.21, 0.52, 0.89)
# Glow and scan beam reach this far out, in sensor radii (clear of "Slide up to unlock" above)
GLOW_REACH = 1.3
# One sweep of the beam from the top of the glow to its bottom
SCAN_PERIOD_US = 1_100_000
# On the sensor itself the beam stays faint: the finger needs the white light
BEAM_ON_SENSOR = (0.62, 0.82, 1.0)
BEAM_ON_SENSOR_ALPHA = 0.35
BEAM_AROUND_ALPHA = 0.95


def display_mode():
    """Width and height of the connected panel in pixels, from DRM (portrait, unrotated)."""
    for status in sorted(glob.glob("/sys/class/drm/card*-*/status")):
        try:
            with open(status) as f:
                if f.read().strip() != "connected":
                    continue
            with open(status[: -len("status")] + "modes") as f:
                w, h = f.readline().strip().split("x")
            return int(w), int(h)
        except (OSError, ValueError):
            continue
    return None


class Backlight:
    """Full brightness while a finger is on the sensor, through logind (no root needed)."""

    def __init__(self, system):
        self.system = system
        self.name = None
        self.saved = None
        self.top = None
        devices = sorted(glob.glob(BACKLIGHT_DIR + "/*"))
        if devices:
            self.name = devices[0].rsplit("/", 1)[1]

    def _read(self, attr):
        with open(f"{BACKLIGHT_DIR}/{self.name}/{attr}") as f:
            return int(f.read())

    def _set(self, value, done=None):
        def finished(connection, result, *_):
            try:
                connection.call_finish(result)
            except GLib.Error as e:
                print(f"plinius-udfps: SetBrightness: {e.message}", file=sys.stderr)
            if done:
                done()

        self.system.call(
            "org.freedesktop.login1", "/org/freedesktop/login1/session/auto",
            "org.freedesktop.login1.Session", "SetBrightness",
            GLib.Variant("(ssu)", ("backlight", self.name, value)),
            None, Gio.DBusCallFlags.NONE, -1, None, finished)

    def dim_alpha(self):
        """Black over the rest of the screen at full brightness that keeps it as bright as now."""
        if not self.name:
            return 0.0
        try:
            level = self.saved if self.saved is not None else self._read("brightness")
            top = self.top if self.top is not None else self._read("max_brightness")
        except (OSError, ValueError):
            return 0.0
        if top <= 0:
            return 0.0
        # Level 0 is still lit (plinius-backlight starts at the panel's lowest visible level)
        fraction = min(1.0, max(level, 1) / top)
        return 1.0 - fraction ** (PANEL_GAMMA / DISPLAY_GAMMA)

    def boost(self):
        if not self.name or self.saved is not None:
            return
        try:
            self.saved = self._read("brightness")
            self.top = self._read("max_brightness")
            self._set(self.top)
        except (OSError, ValueError) as e:
            self.saved = None
            print(f"plinius-udfps: no brightness boost: {e}", file=sys.stderr)

    def restore(self, done=None):
        """The brightness from before the boost; done() once it is set (or right away)."""
        if self.saved is None:
            if done:
                done()
            return
        self._set(self.saved, done)
        self.saved = None


def beam_offset(phase, reach):
    """Height of the scan beam over the sensor centre for a phase 0..1 of the sweep: from the
    top of the glow to its bottom, slower at both ends."""
    ease = (1 - math.cos(math.pi * phase)) / 2
    return reach * (2 * ease - 1)


def draw_beam(cr, left, right, y, trail, rgb, alpha):
    """A line at y with a trail fading out upwards, from left to right (clipped by the caller)."""
    gradient = cairo.LinearGradient(0, y - trail, 0, y)
    gradient.add_color_stop_rgba(0, *rgb, 0)
    gradient.add_color_stop_rgba(1, *rgb, alpha * 0.6)
    cr.rectangle(left, y - trail, right - left, trail)
    cr.set_source(gradient)
    cr.fill()
    width = max(1.5, trail / 10)
    cr.rectangle(left, y - width / 2, right - left, width)
    cr.set_source_rgba(*rgb, alpha)
    cr.fill()


class Glow(Gtk.Window):
    """The whole screen while a finger is on the sensor: dimmed by as much as the backlight goes
    up, the sensor left out, a glow and the scan beam around it. Never takes a touch."""

    def __init__(self, target):
        super().__init__()
        self.target = target
        self.alpha = 0.0

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "plinius-udfps-glow")
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        for edge in (GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT,
                     GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM):
            GtkLayerShell.set_anchor(self, edge, True)
        GtkLayerShell.set_exclusive_zone(self, -1)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)

        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        # The finger stays with the target below: no input anywhere on the veil
        self.input_shape_combine_region(cairo.Region())

        self.area = Gtk.DrawingArea()
        self.area.connect("draw", self.on_draw)
        self.add(self.area)

    def sensor(self, height):
        """Centre and radius of the sensor in the veil's logical pixels, as the target sits."""
        left, bottom, size = self.target.disc
        r = size / 2
        return left + r, height - bottom - r, r

    def queue_beam(self):
        """Redraw only around the sensor, where the beam moves."""
        if self.target.disc is None:
            return
        cx, cy, r = self.sensor(self.area.get_allocated_height())
        reach = math.ceil(GLOW_REACH * r) + 2
        self.area.queue_draw_area(int(cx - reach), int(cy - reach), 2 * reach, 2 * reach)

    def on_draw(self, widget, cr):
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_source_rgba(0, 0, 0, self.alpha)
        cr.paint()
        if self.target.disc is None:
            return False
        cx, cy, r = self.sensor(widget.get_allocated_height())
        reach = GLOW_REACH * r
        # The sensor shows through: the target draws it, above or below the veil (its white
        # circle ends a pixel inside its edge)
        r -= 1
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()
        cr.set_operator(cairo.OPERATOR_OVER)
        # Glow and beam only in the ring around the sensor
        cr.save()
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.arc(cx, cy, reach, 0, 2 * math.pi)
        cr.new_sub_path()
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.clip()
        glow = cairo.RadialGradient(cx, cy, r, cx, cy, reach)
        glow.add_color_stop_rgba(0, *ACCENT, 0.7)
        glow.add_color_stop_rgba(1, *ACCENT, 0)
        cr.set_source(glow)
        cr.paint()
        draw_beam(cr, cx - reach, cx + reach, cy + beam_offset(self.target.scan_phase(), reach),
                  r * 0.6, ACCENT, BEAM_AROUND_ALPHA)
        cr.restore()
        return False


class Target(Gtk.Window):
    def __init__(self, fp, system, x, y, radius, panel):
        super().__init__()
        self.fp = fp
        self.backlight = Backlight(system)
        # Sensor centre and radius in panel pixels, panel size (portrait)
        self.sx, self.sy, self.sr = x, y, radius
        self.panel_w, self.panel_h = panel
        self.state = STATE_IDLE
        self.progress = 0
        self.pressed = False
        # The boost and then UiReady wait on this source
        self.ui_ready_source = 0
        self.tick = 0
        self.scan_start = 0
        # Left and bottom margin and size of the circle in logical pixels, set by place()
        self.disc = None

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "plinius-udfps")
        # Above the lock screen and every app
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
        # Measured from the panel edge, not from Phosh's home bar
        GtkLayerShell.set_exclusive_zone(self, -1)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)

        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)

        self.area = Gtk.DrawingArea()
        self.area.connect("draw", self.on_draw)
        self.area.add_events(Gdk.EventMask.TOUCH_MASK | Gdk.EventMask.BUTTON_PRESS_MASK |
                             Gdk.EventMask.BUTTON_RELEASE_MASK)
        self.area.connect("touch-event", self.on_touch)
        self.area.connect("button-press-event", self.on_button)
        self.area.connect("button-release-event", self.on_button)
        self.add(self.area)

        self.glow = Glow(self)

    # ---- Placement ----

    def builtin_monitor(self):
        """The phone's own panel: the only screen with a physical size.

        Not the primary monitor, and not left to the compositor: with a second
        (virtual) screen the sensor ring would end up there.
        """
        display = self.get_display()
        for i in range(display.get_n_monitors()):
            monitor = display.get_monitor(i)
            if monitor.get_width_mm() > 0 and monitor.get_height_mm() > 0:
                return monitor
        return display.get_primary_monitor() or display.get_monitor(0)

    def place(self):
        """Size and margins in logical pixels; False if the display is not in portrait."""
        monitor = self.builtin_monitor()
        if monitor is None:
            return False
        GtkLayerShell.set_monitor(self, monitor)
        GtkLayerShell.set_monitor(self.glow, monitor)
        geometry = monitor.get_geometry()
        if geometry.width > geometry.height:
            return False
        ratio = self.panel_w / geometry.width
        size = max(1, round(2 * self.sr / ratio))
        left = round((self.sx - self.sr) / ratio)
        bottom = round((self.panel_h - self.sy - self.sr) / ratio)
        self.area.set_size_request(size, size)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT, left)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.BOTTOM, bottom)
        self.disc = (left, bottom, size)
        return True

    # ---- biomd state ----

    def set_state(self, state):
        self.state = state
        if state == STATE_IDLE:
            self.release()
            self.hide()
            return
        if state == STATE_ENROLLING:
            self.progress = 0
        if self.place():
            self.show_all()
        else:
            self.hide()
        self.area.queue_draw()

    def set_progress(self, progress):
        self.progress = progress
        self.area.queue_draw()

    # ---- Touch ----

    def on_touch(self, widget, event):
        if event.type == Gdk.EventType.TOUCH_BEGIN:
            self.press()
        elif event.type in (Gdk.EventType.TOUCH_END, Gdk.EventType.TOUCH_CANCEL):
            self.release()
        return True

    def on_button(self, widget, event):
        # Mouse (and touch emulated as pointer, if the touch path did not take it)
        if event.type == Gdk.EventType.BUTTON_PRESS:
            self.press()
        elif event.type == Gdk.EventType.BUTTON_RELEASE:
            self.release()
        return True

    def press(self):
        if self.pressed or self.state == STATE_IDLE:
            return
        self.pressed = True
        self.scan_start = GLib.get_monotonic_time()
        self.call("PointerDown", GLib.Variant("(ii)", (self.sx, self.sy)))
        self.area.queue_draw()
        self.tick = self.area.add_tick_callback(self.on_tick)
        # The veil first, as dark as the backlight will get brighter
        self.glow.alpha = self.backlight.dim_alpha()
        self.glow.show_all()
        self.glow.area.queue_draw()
        clock = self.glow.get_frame_clock()
        if clock:
            handler = None

            def painted(c):
                c.disconnect(handler)
                if self.pressed and not self.ui_ready_source:
                    self.ui_ready_source = GLib.timeout_add(BOOST_DELAY_MS, self.boost)

            handler = clock.connect("after-paint", painted)
        else:
            self.boost()

    def boost(self):
        """The veil is on screen: full brightness, UiReady once the panel had a moment."""
        self.ui_ready_source = 0
        if not self.pressed:
            return GLib.SOURCE_REMOVE
        self.backlight.boost()
        self.ui_ready_source = GLib.timeout_add(UI_READY_DELAY_MS, self.ui_ready)
        return GLib.SOURCE_REMOVE

    def ui_ready(self):
        self.ui_ready_source = 0
        if self.pressed:
            self.call("UiReady", None)
        return GLib.SOURCE_REMOVE

    def release(self):
        if not self.pressed:
            return
        self.pressed = False
        if self.ui_ready_source:
            GLib.source_remove(self.ui_ready_source)
            self.ui_ready_source = 0
        if self.tick:
            self.area.remove_tick_callback(self.tick)
            self.tick = 0
        self.call("PointerUp", None)
        # The veil goes once the brightness is back, else the screen flashes bright
        self.backlight.restore(self.lift_veil)
        self.area.queue_draw()

    def lift_veil(self):
        # A new press in the meantime keeps it
        if not self.pressed:
            self.glow.hide()

    def call(self, method, params):
        def done(proxy, result):
            try:
                proxy.call_finish(result)
            except GLib.Error as e:
                print(f"plinius-udfps: {method}: {e.message}", file=sys.stderr)

        self.fp.call(method, params, Gio.DBusCallFlags.NONE, -1, None, done)

    # ---- Scan beam ----

    def scan_phase(self):
        """Where the sweep of the beam is, 0..1."""
        elapsed = GLib.get_monotonic_time() - self.scan_start
        return (elapsed % SCAN_PERIOD_US) / SCAN_PERIOD_US

    def on_tick(self, widget, clock):
        self.area.queue_draw()
        self.glow.queue_beam()
        return GLib.SOURCE_CONTINUE

    # ---- Drawing ----

    def on_draw(self, widget, cr):
        w = widget.get_allocated_width()
        r = w / 2
        # Transparent around the circle
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.arc(r, r, r - 1, 0, 2 * math.pi)
        if self.pressed:
            # The light for the optical sensor: white, full circle, the beam passing faintly
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.save()
            cr.clip()
            draw_beam(cr, 0, w, r + beam_offset(self.scan_phase(), GLOW_REACH * r), r * 0.6,
                      BEAM_ON_SENSOR, BEAM_ON_SENSOR_ALPHA)
            cr.restore()
            return False
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.fill_preserve()
        cr.set_line_width(max(2, r / 14))
        cr.set_source_rgba(1, 1, 1, 0.8)
        cr.stroke()
        draw_fingerprint(cr, r)
        if self.state == STATE_ENROLLING and self.progress > 0:
            cr.set_line_width(max(3, r / 9))
            cr.set_source_rgb(*ACCENT)
            cr.arc(r, r, r - r / 14, -math.pi / 2, -math.pi / 2 + 2 * math.pi * self.progress / 100)
            cr.stroke()
        return False


def draw_fingerprint(cr, r):
    """A simple fingerprint mark: concentric open arcs."""
    cr.set_source_rgba(1, 1, 1, 0.9)
    cr.set_line_width(max(1.5, r / 22))
    for i in range(1, 5):
        cr.new_sub_path()
        cr.arc(r, r * 1.1, r * 0.13 * i, math.pi * 1.05, math.pi * 1.95)
        cr.stroke()


def main():
    Gtk.init(sys.argv)
    if not GtkLayerShell.is_supported():
        print("plinius-udfps: the compositor has no layer shell", file=sys.stderr)
        return 1

    system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    panel = display_mode()
    if panel is None:
        print("plinius-udfps: no panel size", file=sys.stderr)
        return 0
    # One target for the whole session, created when biomd first reports a sensor
    current = {"target": None}

    def on_signal(proxy, sender, signal, params):
        target = current["target"]
        if target is None or target.fp is not proxy:
            return
        if signal == "StateChanged":
            target.set_state(params.unpack()[0])
        elif signal == "EnrollmentProgressChanged":
            target.set_progress(params.unpack()[0])

    def on_appeared(connection, name, owner):
        try:
            fp = Gio.DBusProxy.new_sync(system, Gio.DBusProxyFlags.NONE, None,
                                        BIOMD, FP_PATH, FP_IFACE, None)
        except GLib.Error as e:
            print(f"plinius-udfps: biomd: {e.message}", file=sys.stderr)
            return
        sensor_type = fp.get_cached_property("SensorType")
        location = fp.get_cached_property("SensorLocation")
        # An older biomd has neither property
        if sensor_type is None or location is None or sensor_type.unpack() not in UNDER_DISPLAY_TYPES:
            return
        x, y, radius = location.unpack()
        if radius <= 0:
            return
        target = current["target"]
        if target is None:
            target = current["target"] = Target(fp, system, x, y, radius, panel)
        else:
            target.fp = fp
            target.sx, target.sy, target.sr = x, y, radius
        fp.connect("g-signal", on_signal)
        state = fp.get_cached_property("State")
        target.set_state(state.unpack() if state else STATE_IDLE)

    def on_vanished(connection, name):
        if current["target"] is not None:
            current["target"].set_state(STATE_IDLE)

    Gio.bus_watch_name_on_connection(system, BIOMD, Gio.BusNameWatcherFlags.NONE,
                                     on_appeared, on_vanished)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
