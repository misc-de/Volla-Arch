#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""plinius-touch-controls: a stick and buttons on the screen for games that want a keyboard.

DOS games want arrow keys, Ctrl, Alt, Space, Enter and Esc; the on-screen keyboard covers half
the picture and has no stick. This puts two transparent layer-shell surfaces next to the picture
of a 4:3 game: upright below it (stick left, buttons right), lying in the bars left and right of
it. They send keys through the compositor's virtual keyboard (zwp_virtual_keyboard_v1, as Stevia
does), so the game's window keeps the keyboard focus. A touch on them never reaches the game.

They only show while the game's window is the active one (wlr foreign toplevel management), so
nothing of them is left over the overview, another app or the lock screen, and not while the
on-screen keyboard is unfolded (it lies under them). Keys held are let go whenever they hide.

Started by whatever starts the game (the DOS Installer's launchers) and ended with it; it also
ends by itself once the game's window was there and is gone.

The Wayland side speaks the wire protocol itself: pywayland has no virtual keyboard and GTK
does not share its connection. Requests and events are the few this needs.
"""

import argparse
import array
import math
import os
import select
import signal
import socket
import struct
import sys
import time

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, Gio, GLib, Gtk, GtkLayerShell  # noqa: E402

APP_ID = "org.plinius.TouchControls"
NAMESPACE = "plinius-touch-controls"


def log(message):
    print(f"{NAMESPACE}: {message}", file=sys.stderr)


# ----- Keys -----

# Name -> (evdev key code, label on the button)
KEYS = {
    "esc": (1, "Esc"), "tab": (15, "Tab"), "enter": (28, "Enter"), "space": (57, "Space"),
    "backspace": (14, "⌫"), "ctrl": (29, "Ctrl"), "alt": (56, "Alt"), "shift": (42, "Shift"),
    "up": (103, "↑"), "down": (108, "↓"), "left": (105, "←"), "right": (106, "→"),
    "pgup": (104, "PgUp"), "pgdn": (109, "PgDn"), "home": (102, "Home"), "end": (107, "End"),
    "ins": (110, "Ins"), "del": (111, "Del"),
    "kp8": (72, "8"), "kp2": (80, "2"), "kp4": (75, "4"), "kp6": (77, "6"),
    # The number pad's corners and middle: Mortal Kombat's second player fights with them
    "kp7": (71, "7"), "kp9": (73, "9"), "kp1": (79, "1"), "kp3": (81, "3"), "kp5": (76, "5"),
    # The right Shift: Wolfenstein 3D runs with it only, the Doom engine expects it
    "rshift": (54, "Shift"),
    # Hexen jumps with /, the Doom engine's later games page through their inventory with [ ]
    "slash": (53, "/"), "lbracket": (26, "["), "rbracket": (27, "]"),
}
KEYS.update({f"f{n}": (58 + n if n <= 10 else 76 + n, f"F{n}") for n in range(1, 13)})
KEYS.update({str(n): (1 + n if n else 11, str(n)) for n in range(10)})
KEYS.update({c: (code, c.upper()) for c, code in zip(
    "qwertyuiopasdfghjklzxcvbnm",
    (16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 30, 31, 32, 33, 34, 35, 36, 37, 38,
     44, 45, 46, 47, 48, 49, 50))})
# Buttons that are not keys
ACTIONS = {"keyboard"}
# Key code -> modifier mask in the keymap below (xkbcommon's real modifiers come first:
# Shift, Lock, Control, Mod1 = Alt)
MODIFIERS = {42: 1, 54: 1, 29: 4, 97: 4, 56: 8, 100: 8}
# What the stick holds for up, down, left, right
STICKS = {"arrows": ("up", "down", "left", "right"), "wasd": ("w", "s", "a", "d"),
          "numpad": ("kp8", "kp2", "kp4", "kp6")}
DEFAULT_BUTTONS = ("ctrl", "space", "alt", "enter")
DEFAULT_SMALL = ("esc", "keyboard")
# Five for a fighting game: three above, two below, as on an arcade panel
MAX_BUTTONS = 5
MAX_SMALL = 4
# More keys in rows below the small buttons (a game's weapons, say), as many per row. Three
# rows leave the big buttons as large as before lying, where the bars are narrow; upright
# they get smaller.
MAX_KEYS = 12
KEYS_PER_ROW = 4

# US layout, as the Terminal layout of the on-screen keyboard: DOSBox runs with keysyms
# (usescancodes=false). The compositor compiles it with its own XKB data.
KEYMAP = """xkb_keymap {
  xkb_keycodes { include "evdev+aliases(qwerty)" };
  xkb_types { include "complete" };
  xkb_compat { include "complete" };
  xkb_symbols { include "pc+us+inet(evdev)" };
  xkb_geometry { include "pc(pc105)" };
};
"""


class Keys:
    """The keys the controls hold: a key goes down with the first control that holds it and up
    with the last one, the modifiers follow (the compositor does not track them for a virtual
    keyboard)."""

    def __init__(self, keyboard):
        self.keyboard = keyboard
        self.count = {}
        self.mask = 0

    def press(self, code):
        n = self.count.get(code, 0)
        self.count[code] = n + 1
        if n == 0:
            self.keyboard.key(code, True)
            self._modifiers()

    def release(self, code):
        n = self.count.get(code, 0)
        if n == 0:
            return
        if n > 1:
            self.count[code] = n - 1
            return
        del self.count[code]
        self.keyboard.key(code, False)
        self._modifiers()

    def release_all(self):
        for code in list(self.count):
            self.count[code] = 1
            self.release(code)

    def _modifiers(self):
        mask = 0
        for code in self.count:
            mask |= MODIFIERS.get(code, 0)
        if mask != self.mask:
            self.mask = mask
            self.keyboard.modifiers(mask)


# ----- Wayland wire protocol -----

class WaylandError(Exception):
    pass


def u32(*values):
    return struct.pack(f"<{len(values)}I", *values)


def string(text):
    data = text.encode() + b"\0"
    return u32(len(data)) + data + b"\0" * (-len(data) % 4)


class Reader:
    """The arguments of one event."""

    def __init__(self, body):
        self.body = body
        self.pos = 0

    def uint(self):
        if self.pos + 4 > len(self.body):
            raise WaylandError("short event")
        (value,) = struct.unpack_from("<I", self.body, self.pos)
        self.pos += 4
        return value

    def blob(self):
        size = self.uint()
        if self.pos + size > len(self.body):
            raise WaylandError("short event")
        data = self.body[self.pos:self.pos + size]
        self.pos += size + (-size % 4)
        return data

    def string(self):
        return self.blob().rstrip(b"\0").decode(errors="replace")

    def uints(self):
        data = self.blob()
        return list(struct.unpack(f"<{len(data) // 4}I", data[:len(data) // 4 * 4]))


class Wire:
    """One connection to the compositor: object ids, requests out, events in."""

    def __init__(self, sock):
        self.sock = sock
        self.next_id = 2
        # Object id -> handler(opcode, body); id 1 is wl_display
        self.objects = {1: self._display_event}
        self.buffer = b""

    @classmethod
    def connect(cls, env=None):
        env = os.environ if env is None else env
        name = env.get("WAYLAND_DISPLAY") or "wayland-0"
        path = name if name.startswith("/") else os.path.join(env.get("XDG_RUNTIME_DIR", ""), name)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        try:
            sock.connect(path)
        except OSError:
            sock.close()
            raise
        return cls(sock)

    def new_id(self, handler=None):
        oid = self.next_id
        self.next_id += 1
        self.objects[oid] = handler
        return oid

    def send(self, oid, opcode, payload=b"", fds=()):
        message = u32(oid, (8 + len(payload)) << 16 | opcode) + payload
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds).tobytes())] \
            if fds else []
        sent = self.sock.sendmsg([message], ancillary)
        if sent < len(message):
            self.sock.sendall(message[sent:])

    def read(self):
        """Reads what the compositor sent and hands it to the objects; False once it hung up."""
        try:
            data, ancillary, _flags, _addr = self.sock.recvmsg(
                65536, socket.CMSG_SPACE(16 * 4), socket.MSG_DONTWAIT)
        except BlockingIOError:
            return True
        except OSError:
            return False
        for level, kind, fds in ancillary:
            # None of the events this listens to carries a file; close what comes anyway
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                for fd in array.array("i", fds[:len(fds) - len(fds) % 4]):
                    os.close(fd)
        if not data:
            return False
        self.buffer += data
        while len(self.buffer) >= 8:
            oid, word = struct.unpack_from("<II", self.buffer)
            size = word >> 16
            if size < 8:
                raise WaylandError("broken message")
            if len(self.buffer) < size:
                break
            body, self.buffer = self.buffer[8:size], self.buffer[size:]
            handler = self.objects.get(oid)
            if handler:
                handler(word & 0xffff, body)
        return True

    def roundtrip(self, timeout=5.0):
        """Waits until the compositor handled everything sent so far."""
        done = []
        callback = self.new_id(lambda opcode, body: done.append(True))
        self.send(1, 0, u32(callback))  # wl_display.sync
        deadline = time.monotonic() + timeout
        while not done:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.sock], [], [], left)[0]:
                raise WaylandError("the compositor does not answer")
            if not self.read():
                raise WaylandError("the compositor hung up")
        self.objects.pop(callback, None)

    def _display_event(self, opcode, body):
        r = Reader(body)
        if opcode == 0:  # error
            oid, code, message = r.uint(), r.uint(), r.string()
            raise WaylandError(f"error {code} on object {oid}: {message}")
        if opcode == 1:  # delete_id
            self.objects.pop(r.uint(), None)


class Registry:
    """The compositor's globals, and binding them."""

    def __init__(self, wire):
        self.wire = wire
        self.globals = {}
        self.oid = wire.new_id(self._event)
        wire.send(1, 1, u32(self.oid))  # wl_display.get_registry
        wire.roundtrip()

    def _event(self, opcode, body):
        r = Reader(body)
        if opcode == 0:  # global
            name, interface, version = r.uint(), r.string(), r.uint()
            self.globals[interface] = (name, version)
        elif opcode == 1:  # global_remove
            name = r.uint()
            self.globals = {i: g for i, g in self.globals.items() if g[0] != name}

    def bind(self, interface, version, handler=None):
        if interface not in self.globals:
            raise WaylandError(f"the compositor has no {interface}")
        name, offered = self.globals[interface]
        oid = self.wire.new_id(handler)
        self.wire.send(self.oid, 0, u32(name) + string(interface) + u32(min(version, offered), oid))
        return oid


class VirtualKeyboard:
    """zwp_virtual_keyboard_v1 on the compositor's seat, with the US keymap."""

    MANAGER = "zwp_virtual_keyboard_manager_v1"

    def __init__(self, registry, keymap=KEYMAP):
        self.wire = registry.wire
        seat = registry.bind("wl_seat", 1)
        manager = registry.bind(self.MANAGER, 1)
        self.oid = self.wire.new_id()
        self.wire.send(manager, 0, u32(seat, self.oid))  # create_virtual_keyboard
        data = keymap.encode() + b"\0"
        fd = os.memfd_create("plinius-touch-keymap", os.MFD_CLOEXEC)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            self.wire.send(self.oid, 0, u32(1, len(data)), fds=[fd])  # keymap, xkb_v1
        finally:
            os.close(fd)

    @staticmethod
    def _time():
        return int(time.monotonic() * 1000) & 0xffffffff

    def key(self, code, pressed):
        self.wire.send(self.oid, 1, u32(self._time(), code, 1 if pressed else 0))

    def modifiers(self, depressed):
        self.wire.send(self.oid, 2, u32(depressed, 0, 0, 0))


class Toplevels:
    """The windows there are, by app id, and which is active (zwlr_foreign_toplevel_manager_v1).
    changed() is called once a window's state is complete (done) or it closed."""

    MANAGER = "zwlr_foreign_toplevel_manager_v1"
    ACTIVATED = 2
    # Handle request: 4 would be activate, which wants a seat (a protocol error without one)
    DESTROY = 7

    def __init__(self, registry, changed):
        self.wire = registry.wire
        self.changed = changed
        # Handle id -> {"app_id", "activated"}; what came since the last done waits in pending
        self.windows = {}
        self.pending = {}
        self.manager = registry.bind(self.MANAGER, 3, self._manager_event)

    def _manager_event(self, opcode, body):
        if opcode == 0:  # toplevel: a new handle, made by the compositor
            oid = Reader(body).uint()
            self.windows[oid] = {"app_id": "", "activated": False}
            self.pending[oid] = {}
            self.wire.objects[oid] = lambda op, b, oid=oid: self._handle_event(oid, op, b)

    def _handle_event(self, oid, opcode, body):
        r = Reader(body)
        if opcode == 1:  # app_id
            self.pending[oid]["app_id"] = r.string()
        elif opcode == 4:  # state
            self.pending[oid]["activated"] = self.ACTIVATED in r.uints()
        elif opcode == 5:  # done
            self.windows[oid].update(self.pending[oid])
            self.pending[oid] = {}
            self.changed()
        elif opcode == 6:  # closed
            self.windows.pop(oid, None)
            self.pending.pop(oid, None)
            self.wire.objects.pop(oid, None)
            self.wire.send(oid, self.DESTROY)
            self.changed()

    def _of(self, app_id):
        return [w for w in self.windows.values() if w["app_id"].lower() == app_id.lower()]

    def present(self, app_id):
        return bool(self._of(app_id))

    def active(self, app_id):
        return any(w["activated"] for w in self._of(app_id))


# ----- Where the controls go -----

# Logical pixels left free at the bottom edge, for Phosh's swipe up
EDGE = 24
# Smallest pad: on a screen without room next to the picture the pads lie over it
MIN_PAD = 110
# Lying, the bars beside the picture are tall: the stick and the big buttons sit in the
# middle of their lower third, where the thumbs hold the phone, as far down as they fit
LOW = 5 / 6


def low_centre(h, extent):
    """Where the middle of a control reaching extent up and down goes in a pad of height h
    that holds it low: the lower third's middle, moved up until it fits."""
    return max(extent, min(h * LOW, h - extent))


def pads(width, height, aspect=4 / 3):
    """Both pads on a screen of width x height logical pixels around a picture of the given
    aspect that fills the screen's width (upright) or height (lying), centred like sdl12-compat
    puts it: {"stick" | "buttons": (anchors, margins, (w, h))}."""
    if width <= height:
        free = (height - width / aspect) / 2
        h = max(MIN_PAD, int(free) - EDGE)
        w = width // 2
        return {"stick": (("bottom", "left"), {"bottom": EDGE}, (w, h)),
                "buttons": (("bottom", "right"), {"bottom": EDGE}, (width - w, h))}
    free = (width - height * aspect) / 2
    w = max(MIN_PAD, int(free))
    h = height - 2 * EDGE
    return {"stick": (("left",), {}, (w, h)), "buttons": (("right",), {}, (w, h))}


class Button:
    def __init__(self, name, x, y, w, h, round_):
        self.name = name
        self.x, self.y, self.w, self.h = x, y, w, h
        self.round = round_
        self.pressed = False

    @property
    def label(self):
        return KEYS[self.name][1] if self.name in KEYS else ""

    def hit(self, px, py):
        if self.round:
            cx, cy = self.x + self.w / 2, self.y + self.h / 2
            return math.hypot(px - cx, py - cy) <= self.w * 0.6
        slack = 4
        return (self.x - slack <= px <= self.x + self.w + slack
                and self.y - slack <= py <= self.y + self.h + slack)


# Places of the big buttons as (x, y) in units of their distance from the centre: the first
# where the thumb rests (bottom), then right, left, top
DIAMOND = ((0, 1), (1, 0), (-1, 0), (0, -1))
PAIR = ((-0.6, 0.45), (0.6, -0.45))
# Five as on an arcade fighter's panel, in reading order: three above, two below between them,
# as (x, y) in units of the side the buttons share. Each is 0.3 of the side across and at least
# that far from the next; together they reach 0.495 sideways and 0.3 up and down.
ARCADE = ((-0.345, -0.15), (0, -0.15), (0.345, -0.15), (-0.1725, 0.15), (0.1725, 0.15))
ARCADE_R = 0.15


def small_height(h):
    return min(40, max(24, h * 0.18))


def rows(small, extra=()):
    """The rows along the top: the small buttons, then the extra keys, KEYS_PER_ROW to a row."""
    extra = list(extra)
    return ([list(small)] if small else []) + [extra[i:i + KEYS_PER_ROW]
                                               for i in range(0, len(extra), KEYS_PER_ROW)]


def rows_bottom(h, small, extra=()):
    """Where the room below the rows along the top of a pad h high starts."""
    gap = 8
    return gap + len(rows(small, extra)) * (small_height(h) + gap)


def big_side(w, h, small, extra=()):
    """The side of the square the big buttons of a pad of w x h share, below the rows."""
    gap = 8
    return max(0, min(w - 2 * gap, h - rows_bottom(h, small, extra) - gap))


def big_button_size(w, h, big, small, extra=()):
    """How wide (and high) one big button of a pad of w x h is."""
    side = big_side(w, h, small, extra)
    if len(big) == len(ARCADE):
        return side * 2 * ARCADE_R
    return side * 0.6 if len(big) == 1 else side * 0.38 if big else 0


def button_layout(w, h, big, small, low=False, extra=()):
    """The buttons in a pad of w x h: the small ones in a row along the top, the extra keys in
    rows below them, the big ones below as a diamond like a gamepad's (five as on an arcade
    panel), in the middle of the room left or, low, in the pad's lower third (LOW), a button's
    height above its bottom."""
    gap = 8
    out = []
    sh = small_height(h)
    y = gap
    for i, row in enumerate(rows(small, extra)):
        # The extra keys' buttons as wide as in a full row, so that they line up
        count = len(row) if i == 0 and small else min(KEYS_PER_ROW, len(extra))
        bw = min(72, (w - gap * (count + 1)) / count)
        x = (w - (bw * len(row) + gap * (len(row) - 1))) / 2
        for name in row:
            out.append(Button(name, x, y, bw, sh, False))
            x += bw + gap
        y += sh + gap
    top = rows_bottom(h, small, extra)
    area = h - top - gap
    side = big_side(w, h, small, extra)
    cx, cy = w / 2, top + area / 2
    if low:
        # How far the big buttons reach from their middle: a lone one 0.3 of the side, a pair
        # 0.45 * 0.30 + 0.19, a diamond 0.30 + 0.19, five 0.15 + 0.15. Below them a button's
        # height stays free; where that would push them into the rows above, they go down to
        # the edge instead.
        reach = side * {1: 0.3, 2: 0.325, len(ARCADE): 0.3}.get(len(big), 0.49)
        free = max(gap, big_button_size(w, h, big, small, extra))
        cy = low_centre(h, reach + free)
        if cy - reach < top:
            cy = low_centre(h, reach + gap)
        cy = max(top + reach, cy)
    if len(big) == 1:
        r = side * 0.3
        out.append(Button(big[0], cx - r, cy - r, 2 * r, 2 * r, True))
    elif len(big) == len(ARCADE):
        r = side * ARCADE_R
        for name, (ux, uy) in zip(big, ARCADE):
            bx, by = cx + ux * side, cy + uy * side
            out.append(Button(name, bx - r, by - r, 2 * r, 2 * r, True))
    elif big:
        r, d = side * 0.19, side * 0.30
        places = PAIR if len(big) == 2 else DIAMOND
        for name, (ux, uy) in zip(big, places):
            bx, by = cx + ux * d, cy + uy * d
            out.append(Button(name, bx - r, by - r, 2 * r, 2 * r, True))
    return out


def stick_directions(dx, dy, radius, dead=0.3):
    """Which of up/down/left/right a stick pushed by (dx, dy) holds, in eight sectors of 45°
    (two at once on the diagonals); screen y grows downwards."""
    length = math.hypot(dx, dy)
    if radius <= 0 or length < dead * radius:
        return set()
    edge = length * math.sin(math.radians(22.5))
    out = set()
    if dx > edge:
        out.add("right")
    if dx < -edge:
        out.add("left")
    if dy < -edge:
        out.add("up")
    if dy > edge:
        out.add("down")
    return out


# ----- The controls, without GTK -----

# The two looks of the direction pad: a stick whose centre goes where the thumb comes down,
# and a fixed cross like a gamepad's
STYLES = ("stick", "dpad")


def style_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, NAMESPACE, "style")


def load_style():
    """The style chosen last, kept across games."""
    try:
        with open(style_path()) as f:
            style = f.read().strip()
    except OSError:
        return STYLES[0]
    return style if style in STYLES else STYLES[0]


def save_style(style):
    path = style_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(style + "\n")
    except OSError as e:
        log(f"style not kept: {e}")


class Stick:
    """The direction keys, as a stick or a cross (style); the small button in the corner
    switches between them.

    stick: one finger anywhere on the pad, the stick's centre goes where it touched, pushing
    it holds the direction keys. dpad: a fixed cross in the middle of the pad, the direction
    from its centre counts wherever the finger is."""

    def __init__(self, keys, names, feedback=lambda: None, style=STYLES[0],
                 styled=lambda style: None):
        self.keys = keys
        self.codes = {d: KEYS[n][0] for d, n in zip(("up", "down", "left", "right"), names)}
        self.feedback = feedback
        self.style = style if style in STYLES else STYLES[0]
        self.styled = styled
        self.w = self.h = 0
        self.low = False
        # Lying, room kept free below the stick at rest: as below Ctrl (Overlay.place)
        self.lift = 0
        self.radius = 0
        self.touch = None
        self.centre = self.knob = (0, 0)
        self.held = set()
        self.toggle = Button("style", 0, 0, 0, 0, True)
        self.toggle_touch = None

    def resize(self, w, h, low=None):
        """A pad of w x h; low (kept when None) puts the stick at rest in its lower third."""
        self.w, self.h = w, h
        if low is not None:
            self.low = low
        size = min(44, max(28, min(w, h) * 0.2))
        self.toggle = Button("style", 8, 8, size, size, True)
        self.radius = min(w, h) * (0.42 if self.style == "dpad" else 0.36)
        if self.touch is None:
            self.centre = self.knob = self.rest

    @property
    def rest(self):
        """The middle of the stick or cross while no finger holds it."""
        if self.low:
            return (self.w / 2, low_centre(self.h, self.radius + max(8, self.lift)))
        return (self.w / 2, self.h / 2)

    def down(self, seq, x, y):
        if self.touch is not None or self.toggle_touch is not None:
            return False
        if self.toggle.hit(x, y):
            self.toggle_touch = seq
            self.toggle.pressed = True
            self.feedback()
            self.style = STYLES[(STYLES.index(self.style) + 1) % len(STYLES)]
            self.resize(self.w, self.h)
            self.toggle.pressed = True
            self.styled(self.style)
            return True
        self.touch = seq
        if self.style == "stick":
            r = self.radius
            self.centre = (min(max(x, r), max(r, self.w - r)),
                           min(max(y, r), max(r, self.h - r)))
        self.move(seq, x, y)
        return True

    def move(self, seq, x, y):
        if seq == self.toggle_touch:
            return True
        if seq != self.touch:
            return False
        dx, dy = x - self.centre[0], y - self.centre[1]
        if self.style == "dpad":
            # A smaller middle: the cross has no travel, a slight roll of the thumb counts
            self._hold(stick_directions(dx, dy, self.radius, dead=0.15))
            return True
        length = math.hypot(dx, dy)
        if length > self.radius > 0:
            dx, dy = dx * self.radius / length, dy * self.radius / length
        self.knob = (self.centre[0] + dx, self.centre[1] + dy)
        self._hold(stick_directions(x - self.centre[0], y - self.centre[1], self.radius))
        return True

    def up(self, seq):
        if self.toggle_touch is not None and seq in (self.toggle_touch, None):
            self.toggle_touch = None
            self.toggle.pressed = False
            if seq is not None:
                return True
        if self.touch is None or (seq != self.touch and seq is not None):
            return False
        self.touch = None
        self._hold(set())
        self.centre = self.knob = self.rest
        return True

    def release_all(self):
        self.up(None)

    def _hold(self, directions):
        for d in self.held - directions:
            self.keys.release(self.codes[d])
        for d in directions - self.held:
            self.keys.press(self.codes[d])
        if directions and not self.held:
            self.feedback()
        self.held = directions


class Buttons:
    """Each finger holds the button it came down on until it lifts."""

    def __init__(self, keys, big, small, actions, feedback=lambda: None, extra=()):
        self.keys = keys
        self.big, self.small, self.extra = list(big), list(small), list(extra)
        self.actions = actions
        self.feedback = feedback
        self.buttons = []
        self.touches = {}

    def resize(self, w, h, low=False):
        self.release_all()
        self.buttons = button_layout(w, h, self.big, self.small, low, self.extra)

    def down(self, seq, x, y):
        # A touch may reach two buttons that lie close (five big ones): the nearer one counts
        hits = [b for b in self.buttons if b.hit(x, y)]
        if not hits:
            return False
        b = min(hits, key=lambda b: math.hypot(x - b.x - b.w / 2, y - b.y - b.h / 2))
        self.touches[seq] = b
        b.pressed = True
        self.feedback()
        if b.name in self.actions:
            self.actions[b.name]()
        else:
            self.keys.press(KEYS[b.name][0])
        return True

    def move(self, seq, x, y):
        return seq in self.touches

    def up(self, seq):
        b = self.touches.pop(seq, None)
        if b is None:
            return False
        if b not in self.touches.values():
            b.pressed = False
        if b.name not in self.actions:
            self.keys.release(KEYS[b.name][0])
        return True

    def release_all(self):
        for seq in list(self.touches):
            self.up(seq)


# ----- Drawing -----

LINE = (1, 1, 1, 0.45)
FILL = (1, 1, 1, 0.12)
FILL_HELD = (1, 1, 1, 0.45)
TEXT = (1, 1, 1, 0.85)


DIRECTIONS = (("right", 0), ("down", 90), ("left", 180), ("up", 270))


def draw_arrows(cr, cx, cy, r, held, tip=0.88, base=0.72, side=0.1):
    """The four directions as small triangles, lit while held."""
    for d, angle in DIRECTIONS:
        a = math.radians(angle)
        cr.move_to(cx + math.cos(a) * r * tip, cy + math.sin(a) * r * tip)
        cr.line_to(cx + math.cos(a) * r * base - math.sin(a) * r * side,
                   cy + math.sin(a) * r * base + math.cos(a) * r * side)
        cr.line_to(cx + math.cos(a) * r * base + math.sin(a) * r * side,
                   cy + math.sin(a) * r * base - math.cos(a) * r * side)
        cr.close_path()
        cr.set_source_rgba(*(TEXT if d in held else LINE))
        cr.fill()


def cross(cr, cx, cy, r, arm):
    """A plus of half-length r and arm width 2 * arm, as one outline."""
    a = arm
    cr.move_to(cx - a, cy - r)
    for x, y in ((a, -r), (a, -a), (r, -a), (r, a), (a, a), (a, r), (-a, r), (-a, a),
                 (-r, a), (-r, -a), (-a, -a)):
        cr.line_to(cx + x, cy + y)
    cr.close_path()


def draw_toggle(cr, stick):
    """The corner button, showing the style a tap switches to."""
    b = stick.toggle
    cx, cy, s = b.x + b.w / 2, b.y + b.h / 2, b.w / 2
    cr.new_path()
    cr.set_line_width(2)
    cr.arc(cx, cy, s, 0, 2 * math.pi)
    cr.set_source_rgba(*(FILL_HELD if b.pressed else FILL))
    cr.fill_preserve()
    cr.set_source_rgba(*LINE)
    cr.stroke()
    cr.set_source_rgba(*TEXT)
    cr.set_line_width(1.5)
    if stick.style == "stick":
        cross(cr, cx, cy, s * 0.6, s * 0.2)
        cr.fill()
    else:
        cr.arc(cx, cy, s * 0.55, 0, 2 * math.pi)
        cr.stroke()
        cr.arc(cx, cy, s * 0.25, 0, 2 * math.pi)
        cr.fill()


def draw_dpad(cr, stick):
    cx, cy = stick.centre
    r = stick.radius
    arm = r * 0.34
    cr.new_path()
    cr.set_line_width(2)
    cross(cr, cx, cy, r, arm)
    cr.set_source_rgba(*FILL)
    cr.fill_preserve()
    cr.set_source_rgba(*LINE)
    cr.stroke()
    # The arms held, filled from the middle square outwards
    for d, angle in DIRECTIONS:
        if d not in stick.held:
            continue
        a = math.radians(angle)
        ux, uy = round(math.cos(a)), round(math.sin(a))
        x0, x1 = sorted((cx + ux * arm - uy * arm, cx + ux * r + uy * arm))
        y0, y1 = sorted((cy + uy * arm - ux * arm, cy + uy * r + ux * arm))
        cr.rectangle(x0, y0, x1 - x0, y1 - y0)
        cr.set_source_rgba(*FILL_HELD)
        cr.fill()
    draw_arrows(cr, cx, cy, r, stick.held, tip=0.86, base=0.62, side=0.13)


def draw_stick(cr, stick):
    draw_toggle(cr, stick)
    if stick.style == "dpad":
        draw_dpad(cr, stick)
        return
    cx, cy = stick.centre
    r = stick.radius
    cr.new_path()
    cr.set_line_width(2)
    cr.arc(cx, cy, r, 0, 2 * math.pi)
    cr.set_source_rgba(*FILL)
    cr.fill_preserve()
    cr.set_source_rgba(*LINE)
    cr.stroke()
    draw_arrows(cr, cx, cy, r, stick.held)
    kx, ky = stick.knob
    cr.arc(kx, ky, r * 0.42, 0, 2 * math.pi)
    cr.set_source_rgba(*(FILL_HELD if stick.touch is not None else FILL))
    cr.fill_preserve()
    cr.set_source_rgba(*LINE)
    cr.stroke()


def rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def draw_keyboard_icon(cr, cx, cy, size):
    w, h = size, size * 0.62
    cr.set_line_width(1.5)
    rounded(cr, cx - w / 2, cy - h / 2, w, h, size * 0.12)
    cr.stroke()
    key = size * 0.1
    for row, count in ((0, 5), (1, 5)):
        y = cy - h / 2 + h * (0.22 + row * 0.28)
        for i in range(count):
            x = cx - w / 2 + w * (0.14 + i * 0.18)
            cr.rectangle(x - key / 2, y - key / 2, key, key)
    cr.rectangle(cx - w * 0.25, cy + h * 0.18, w * 0.5, key)
    cr.fill()


def draw_buttons(cr, buttons):
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    for b in buttons:
        cr.set_line_width(2)
        # show_text leaves the current point at the label's end: no line from there to the arc
        cr.new_path()
        if b.round:
            cr.arc(b.x + b.w / 2, b.y + b.h / 2, b.w / 2, 0, 2 * math.pi)
        else:
            rounded(cr, b.x, b.y, b.w, b.h, b.h / 2)
        cr.set_source_rgba(*(FILL_HELD if b.pressed else FILL))
        cr.fill_preserve()
        cr.set_source_rgba(*LINE)
        cr.stroke()
        cr.set_source_rgba(*TEXT)
        cx, cy = b.x + b.w / 2, b.y + b.h / 2
        if b.name == "keyboard":
            draw_keyboard_icon(cr, cx, cy, min(b.w * 0.6, b.h * 1.1))
            continue
        label = b.label
        size = (b.w * 0.26 if b.round else b.h * 0.42)
        if len(label) > 3:
            size *= 0.8
        cr.set_font_size(max(8, size))
        ext = cr.text_extents(label)
        cr.move_to(cx - ext.x_bearing - ext.width / 2, cy - ext.y_bearing - ext.height / 2)
        cr.show_text(label)


# ----- The surfaces -----

class Pad(Gtk.Window):
    """One transparent layer-shell surface over everything, holding one control."""

    def __init__(self, control, draw):
        super().__init__()
        self.control = control
        self.draw_control = draw
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, NAMESPACE)
        # Over the fullscreen game: phoc draws and hit-tests the overlay layer above it
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_exclusive_zone(self, -1)
        # A touch must not take the keyboard focus from the game
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.area = Gtk.DrawingArea()
        self.area.add_events(Gdk.EventMask.TOUCH_MASK | Gdk.EventMask.BUTTON_PRESS_MASK |
                             Gdk.EventMask.BUTTON_RELEASE_MASK |
                             Gdk.EventMask.BUTTON1_MOTION_MASK)
        self.area.connect("draw", self.on_draw)
        self.area.connect("touch-event", self.on_touch)
        self.area.connect("button-press-event", self.on_button)
        self.area.connect("button-release-event", self.on_button)
        self.area.connect("motion-notify-event", self.on_motion)
        self.add(self.area)

    def place(self, monitor, anchors, margins, size, low=False):
        """Puts the pad on the monitor; low holds its control in the lower third."""
        GtkLayerShell.set_monitor(self, monitor)
        for name, edge in (("top", GtkLayerShell.Edge.TOP), ("bottom", GtkLayerShell.Edge.BOTTOM),
                           ("left", GtkLayerShell.Edge.LEFT), ("right", GtkLayerShell.Edge.RIGHT)):
            GtkLayerShell.set_anchor(self, edge, name in anchors)
            GtkLayerShell.set_margin(self, edge, margins.get(name, 0))
        w, h = size
        self.area.set_size_request(w, h)
        self.resize(w, h)
        self.control.resize(w, h, low)
        self.area.queue_draw()

    def on_draw(self, widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        self.draw_control(cr, self.control)
        return True

    def on_touch(self, widget, event):
        # GdkEventSequence wrappers compare and hash by the sequence they wrap
        seq = event.sequence
        if event.type == Gdk.EventType.TOUCH_BEGIN:
            changed = self.control.down(seq, event.x, event.y)
        elif event.type == Gdk.EventType.TOUCH_UPDATE:
            changed = self.control.move(seq, event.x, event.y)
        elif event.type in (Gdk.EventType.TOUCH_END, Gdk.EventType.TOUCH_CANCEL):
            changed = self.control.up(seq)
        else:
            changed = False
        if changed:
            self.area.queue_draw()
        return True

    def on_button(self, widget, event):
        # The mouse, and a touch emulated as pointer if the touch path did not take it
        if event.button != 1:
            return True
        if event.type == Gdk.EventType.BUTTON_PRESS:
            changed = self.control.down("pointer", event.x, event.y)
        elif event.type == Gdk.EventType.BUTTON_RELEASE:
            changed = self.control.up("pointer")
        else:
            changed = False
        if changed:
            self.area.queue_draw()
        return True

    def on_motion(self, widget, event):
        if self.control.move("pointer", event.x, event.y):
            self.area.queue_draw()
        return True

    def release_all(self):
        self.control.release_all()
        self.area.queue_draw()


def builtin_monitor(display):
    """The phone's own panel: the only screen with a physical size (as plinius-udfps)."""
    for i in range(display.get_n_monitors()):
        monitor = display.get_monitor(i)
        if monitor.get_width_mm() > 0 and monitor.get_height_mm() > 0:
            return monitor
    return display.get_primary_monitor() or display.get_monitor(0)


OSK = ("sm.puri.OSK0", "/sm/puri/OSK0", "sm.puri.OSK0")


class Overlay:
    """Both pads, shown while the game is the active window and the OSK is folded."""

    def __init__(self, keys, app_id, stick, big, small, aspect=4 / 3, session=None, extra=()):
        self.keys = keys
        self.app_id = app_id
        self.aspect = aspect
        self.session = session
        self.game_active = True
        self.game_seen = False
        self.osk_visible = False
        self.shown = False
        self.monitor = None
        self.osk = None
        self.stick = Stick(keys, STICKS[stick], self.feedback, load_style(), save_style)
        self.buttons = Buttons(keys, big, small, {"keyboard": self.show_keyboard}, self.feedback,
                               extra)
        self.pads = {"stick": Pad(self.stick, draw_stick),
                     "buttons": Pad(self.buttons, lambda cr, c: draw_buttons(cr, c.buttons))}

    # -- placement --

    def attach(self, display):
        self.monitor = builtin_monitor(display)
        if self.monitor is None:
            return False
        self.monitor.connect("notify::geometry", lambda *a: self.place())
        self.place()
        return True

    def place(self):
        geometry = self.monitor.get_geometry()
        # Lying the pads are the tall bars beside the picture: their controls go low
        low = geometry.width > geometry.height
        places = pads(geometry.width, geometry.height, self.aspect)
        # The stick rests as high above the bottom as the lowest big button (Ctrl): a button's
        # height, or at the edge where there is no room for that
        w, h = places["buttons"][2]
        big = [b for b in button_layout(w, h, self.buttons.big, self.buttons.small, low,
                                        self.buttons.extra) if b.round]
        self.stick.lift = h - max(b.y + b.h for b in big) if low and big else 0
        for side, (anchors, margins, size) in places.items():
            self.pads[side].place(self.monitor, anchors, margins, size, low)

    # -- visibility --

    def update(self):
        show = self.game_active and not self.osk_visible
        if show == self.shown:
            return
        self.shown = show
        for pad in self.pads.values():
            pad.release_all()
            if show:
                pad.show_all()
            else:
                pad.hide()
        self.keys.release_all()

    def windows_changed(self, toplevels):
        """From the foreign toplevel list; True while the game runs."""
        present = toplevels.present(self.app_id)
        self.game_seen = self.game_seen or present
        self.game_active = toplevels.active(self.app_id)
        self.update()
        return present or not self.game_seen

    # -- the on-screen keyboard --

    def watch_osk(self):
        if self.session is None:
            return
        Gio.DBusProxy.new(self.session, Gio.DBusProxyFlags.NONE, None, *OSK, None,
                          self._osk_ready)

    def _osk_ready(self, source, result):
        try:
            self.osk = Gio.DBusProxy.new_finish(result)
        except GLib.Error as e:
            log(f"on-screen keyboard: {e.message}")
            return
        self.osk.connect("g-properties-changed", lambda *a: self._osk_changed())
        self._osk_changed()

    def _osk_changed(self):
        visible = self.osk.get_cached_property("Visible")
        self.osk_visible = bool(visible and visible.unpack())
        self.update()

    def show_keyboard(self):
        if self.session is None:
            return
        self.session.call(OSK[0], OSK[1], OSK[2], "SetVisible", GLib.Variant("(b)", (True,)),
                          None, Gio.DBusCallFlags.NONE, 2000, None, self._called, "SetVisible")

    # -- feedback --

    def feedback(self):
        if self.session is None:
            return
        self.session.call("org.sigxcpu.Feedback", "/org/sigxcpu/Feedback", "org.sigxcpu.Feedback",
                          "TriggerFeedback",
                          GLib.Variant("(ssa{sv}i)", (APP_ID, "button-pressed", {}, -1)),
                          None, Gio.DBusCallFlags.NONE, 1000, None, self._called, "Feedback")

    @staticmethod
    def _called(connection, result, what):
        try:
            connection.call_finish(result)
        except GLib.Error as e:
            log(f"{what}: {e.message}")


# ----- Program -----

def key_list(text, limit, allowed):
    """Names from a comma list, the known ones only, at most limit."""
    out = []
    for name in (n.strip().lower() for n in text.split(",")):
        if not name:
            continue
        if name not in allowed:
            log(f"no such key: {name}")
            continue
        if name not in out:
            out.append(name)
    return out[:limit]


def aspect_ratio(text):
    w, sep, h = text.partition(":")
    try:
        value = float(w) / float(h) if sep else float(w)
    except (ValueError, ZeroDivisionError):
        raise argparse.ArgumentTypeError(f"not an aspect ratio: {text}") from None
    if not 0.2 < value < 5:
        raise argparse.ArgumentTypeError(f"not an aspect ratio: {text}")
    return value


def parse_args(argv):
    p = argparse.ArgumentParser(prog=NAMESPACE, description="Touch stick and buttons for a game")
    p.add_argument("--app-id", default="dosbox",
                   help="the game window's app id: shown while it is active, ends with it")
    p.add_argument("--stick", choices=sorted(STICKS), default="arrows")
    p.add_argument("--buttons", default=",".join(DEFAULT_BUTTONS),
                   help=f"up to {MAX_BUTTONS} big buttons, first where the thumb rests "
                   "(five: three above, two below, in reading order)")
    p.add_argument("--small", default=",".join(DEFAULT_SMALL),
                   help=f"up to {MAX_SMALL} small buttons along the top ('keyboard': the OSK)")
    p.add_argument("--keys", default="",
                   help=f"up to {MAX_KEYS} more keys below the small ones, {KEYS_PER_ROW} a row")
    p.add_argument("--aspect", type=aspect_ratio, default=4 / 3,
                   help="shape of the game's picture (default 4:3)")
    args = p.parse_args(argv)
    args.buttons = key_list(args.buttons, MAX_BUTTONS, KEYS)
    args.small = key_list(args.small, MAX_SMALL, set(KEYS) | ACTIONS)
    args.keys = key_list(args.keys, MAX_KEYS, KEYS)
    return args


# Seconds the game window may be gone before the controls end with it
GONE_GRACE = 5


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    Gtk.init([])
    if not GtkLayerShell.is_supported():
        log("the compositor has no layer shell")
        return 1
    try:
        wire = Wire.connect()
        registry = Registry(wire)
        keyboard = VirtualKeyboard(registry)
    except (OSError, WaylandError) as e:
        log(f"no virtual keyboard: {e}")
        return 1
    keys = Keys(keyboard)
    try:
        session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as e:
        log(f"no session bus: {e.message}")
        session = None
    overlay = Overlay(keys, args.app_id, args.stick, args.buttons, args.small, args.aspect,
                      session, args.keys)
    if not overlay.attach(Gdk.Display.get_default()):
        log("no screen")
        return 1
    overlay.watch_osk()

    status = {"code": 0}

    def quit_(code=0):
        status["code"] = code
        overlay.game_active = False
        overlay.update()
        Gtk.main_quit()
        return False

    if Toplevels.MANAGER in registry.globals:
        overlay.game_active = False
        gone = {"timer": 0}

        def gone_for_good():
            gone["timer"] = 0
            if not toplevels.present(args.app_id):
                log(f"{args.app_id} is gone")
                quit_()
            return False

        def changed():
            if overlay.windows_changed(toplevels):
                if gone["timer"]:
                    GLib.source_remove(gone["timer"])
                    gone["timer"] = 0
            elif not gone["timer"]:
                # Not right away: DOSBox (sdl12-compat) closes its first window and opens
                # another one while it starts
                gone["timer"] = GLib.timeout_add_seconds(GONE_GRACE, gone_for_good)
        toplevels = Toplevels(registry, changed)
    else:
        log("no window list: the controls stay")
    overlay.update()

    def on_wayland(fd, condition):
        try:
            alive = wire.read()
        except WaylandError as e:
            log(str(e))
            alive = False
        if not alive:
            quit_(1)
            return False
        return True

    GLib.io_add_watch(wire.sock.fileno(), GLib.PRIORITY_DEFAULT,
                      GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR, on_wayland)
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, quit_)
    Gtk.main()
    # Let go of everything before the connection goes
    keys.release_all()
    return status["code"]


if __name__ == "__main__":
    sys.exit(main())
