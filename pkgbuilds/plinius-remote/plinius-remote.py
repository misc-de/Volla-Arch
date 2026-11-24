#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Remote Input: use the phone as touchpad and keyboard for another computer, TV or phone.

The phone shows up as one Bluetooth Low Energy input device (HID over GATT, "HOGP") with a
keyboard, a mouse and a few media keys. Everything goes through BlueZ's D-Bus API: the HID,
battery and device information services are exported as a GATT application
(org.bluez.GattManager1), the phone advertises itself as an input device
(org.bluez.LEAdvertisingManager1) and answers pairing requests with its own agent
(org.bluez.AgentManager1). Classic Bluetooth HID is not used, so BlueZ's input plugin keeps
working for real keyboards and mice.

Nothing is registered while the switch in the header bar is off: the phone is then neither
visible nor pairable through this app, and BlueZ's adapter settings are as before.

"--desktop": the touchpad and keyboard work this phone's own desktop on a TV instead (Phosh's
cast quick setting starts the app like this once the TV shows it, through Schwupp and
plinius-desktop). The same reports go to a virtual mouse and keyboard (uinput), in full
screen; the app ends when the TV no longer shows the desktop.
"""

import gettext
import math
import os
import signal
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

APP_ID = "org.plinius.Remote"

# UI strings go through gettext so they can be translated later; English is the default.
_ = gettext.translation("plinius-remote", fallback=True).gettext

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
ADV_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
SERVICE_IFACE = "org.bluez.GattService1"
CHRC_IFACE = "org.bluez.GattCharacteristic1"
DESC_IFACE = "org.bluez.GattDescriptor1"
ADV_IFACE = "org.bluez.LEAdvertisement1"
AGENT_IFACE = "org.bluez.Agent1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"

# Our objects on the system bus. BlueZ walks APP_PATH with GetManagedObjects.
APP_PATH = "/org/plinius/remote"
ADV_PATH = APP_PATH + "/advertisement"
AGENT_PATH = APP_PATH + "/agent"
# The start steps that register something, and the flag that has stop() undo it
START_MARKS = {"RegisterAgent": "agent_registered", "RegisterApplication": "app_registered"}

BATTERY = "/sys/class/power_supply/battery/capacity"
SETTINGS_FILE = os.path.join(GLib.get_user_config_dir(), "plinius-remote", "settings.ini")

# GAP appearance "Human Interface Device (generic)": a combo of keyboard and mouse
APPEARANCE_HID = 0x03C0

# Legacy advertising has 31 bytes; flags, the HID service UUID and the appearance take 11 of
# them, the name gets what is left (2 bytes are its own header)
MAX_NAME_BYTES = 18


def uuid16(short):
    """The full 128-bit UUID of a 16-bit Bluetooth SIG UUID, as BlueZ expects it."""
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


# ----- HID report map -----
#
# One descriptor, three top-level collections, told apart by their Report ID. Each input
# report is a GATT Report characteristic of its own; its Report Reference descriptor (0x2908)
# carries the same ID, which is how the host matches characteristic and descriptor part.

RID_KEYBOARD = 1   # 8 bytes in: modifiers, reserved, 6 key slots; 1 byte out: LEDs
RID_MOUSE = 2      # 7 bytes in: buttons, X (int16), Y (int16), wheel (int8), pan (int8)
RID_CONSUMER = 3   # 2 bytes in: one consumer usage (media and TV keys)

REPORT_MAP = bytes([
    # --- Keyboard ---
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x06,        # Usage (Keyboard)
    0xA1, 0x01,        # Collection (Application)
    0x85, RID_KEYBOARD,  # Report ID
    0x05, 0x07,        #   Usage Page (Keyboard/Keypad)
    0x19, 0xE0,        #   Usage Minimum (Left Control)
    0x29, 0xE7,        #   Usage Maximum (Right GUI)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x08,        #   Report Count (8)
    0x81, 0x02,        #   Input (Data, Variable, Absolute): one bit per modifier
    0x95, 0x01,        #   Report Count (1)
    0x75, 0x08,        #   Report Size (8)
    0x81, 0x01,        #   Input (Constant): reserved byte, as in the boot keyboard report
    0x95, 0x05,        #   Report Count (5)
    0x75, 0x01,        #   Report Size (1)
    0x05, 0x08,        #   Usage Page (LEDs)
    0x19, 0x01,        #   Usage Minimum (Num Lock)
    0x29, 0x05,        #   Usage Maximum (Kana)
    0x91, 0x02,        #   Output (Data, Variable, Absolute): LED bits from the host
    0x95, 0x01,        #   Report Count (1)
    0x75, 0x03,        #   Report Size (3)
    0x91, 0x01,        #   Output (Constant): pad the LEDs to a byte
    0x95, 0x06,        #   Report Count (6)
    0x75, 0x08,        #   Report Size (8)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x65,        #   Logical Maximum (101)
    0x05, 0x07,        #   Usage Page (Keyboard/Keypad)
    0x19, 0x00,        #   Usage Minimum (0)
    0x29, 0x65,        #   Usage Maximum (101)
    0x81, 0x00,        #   Input (Data, Array): up to six keys at once
    0xC0,              # End Collection
    # --- Mouse ---
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x02,        # Usage (Mouse)
    0xA1, 0x01,        # Collection (Application)
    0x85, RID_MOUSE,   # Report ID
    0x09, 0x01,        #   Usage (Pointer)
    0xA1, 0x00,        #   Collection (Physical)
    0x05, 0x09,        #     Usage Page (Button)
    0x19, 0x01,        #     Usage Minimum (1)
    0x29, 0x03,        #     Usage Maximum (3)
    0x15, 0x00,        #     Logical Minimum (0)
    0x25, 0x01,        #     Logical Maximum (1)
    0x95, 0x03,        #     Report Count (3)
    0x75, 0x01,        #     Report Size (1)
    0x81, 0x02,        #     Input (Data, Variable, Absolute): left, right, middle
    0x95, 0x01,        #     Report Count (1)
    0x75, 0x05,        #     Report Size (5)
    0x81, 0x01,        #     Input (Constant): pad the buttons to a byte
    0x05, 0x01,        #     Usage Page (Generic Desktop)
    0x09, 0x30,        #     Usage (X)
    0x09, 0x31,        #     Usage (Y)
    0x16, 0x01, 0x80,  #     Logical Minimum (-32767)
    0x26, 0xFF, 0x7F,  #     Logical Maximum (32767)
    0x75, 0x10,        #     Report Size (16): fast swipes do not get clipped
    0x95, 0x02,        #     Report Count (2)
    0x81, 0x06,        #     Input (Data, Variable, Relative)
    0x09, 0x38,        #     Usage (Wheel)
    0x15, 0x81,        #     Logical Minimum (-127)
    0x25, 0x7F,        #     Logical Maximum (127)
    0x75, 0x08,        #     Report Size (8)
    0x95, 0x01,        #     Report Count (1)
    0x81, 0x06,        #     Input (Data, Variable, Relative): vertical wheel
    0x05, 0x0C,        #     Usage Page (Consumer)
    0x0A, 0x38, 0x02,  #     Usage (AC Pan)
    0x95, 0x01,        #     Report Count (1)
    0x75, 0x08,        #     Report Size (8)
    0x81, 0x06,        #     Input (Data, Variable, Relative): horizontal wheel
    0xC0,              #   End Collection
    0xC0,              # End Collection
    # --- Consumer control (volume, playback, Back and Home for TVs) ---
    0x05, 0x0C,        # Usage Page (Consumer)
    0x09, 0x01,        # Usage (Consumer Control)
    0xA1, 0x01,        # Collection (Application)
    0x85, RID_CONSUMER,  # Report ID
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x03,  #   Logical Maximum (1023)
    0x19, 0x00,        #   Usage Minimum (0)
    0x2A, 0xFF, 0x03,  #   Usage Maximum (1023)
    0x75, 0x10,        #   Report Size (16)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x00,        #   Input (Data, Array): the usage that is pressed, 0 = none
    0xC0,              # End Collection
])

# Report lengths without the Report ID (GATT carries the ID in the Report Reference instead)
REPORT_LENGTHS = {(RID_KEYBOARD, "input"): 8, (RID_KEYBOARD, "output"): 1,
                  (RID_MOUSE, "input"): 7, (RID_CONSUMER, "input"): 2}

# ----- Keys (USB HID usage table, US layout) -----

MOD_CTRL, MOD_SHIFT, MOD_ALT, MOD_SUPER = 0x01, 0x02, 0x04, 0x08

KEY_ENTER, KEY_ESC, KEY_BACKSPACE, KEY_TAB = 0x28, 0x29, 0x2A, 0x2B
KEY_RIGHT, KEY_LEFT, KEY_DOWN, KEY_UP = 0x4F, 0x50, 0x51, 0x52

# ----- Full keyboard (tenkeyless, no number pad) -----

MOD_RCTRL, MOD_RALT = 0x10, 0x40
KEY_DELETE, KEY_HOME, KEY_END, KEY_PAGEUP, KEY_PAGEDOWN = 0x4C, 0x4A, 0x4D, 0x4B, 0x4E
KEY_CAPS, KEY_SPACE, KEY_F1 = 0x39, 0x2C, 0x3A

# Labels of the keys that differ between layouts; HID usages are positions, the host's layout
# decides what a key types, so the labels should match the host's layout
LABELS_US = {0x35: "`", 0x2D: "-", 0x2E: "=", 0x2F: "[", 0x30: "]", 0x31: "\\", 0x33: ";",
             0x34: "'", 0x36: ",", 0x37: ".", 0x38: "/"}
LABELS_DE = {0x35: "^", 0x2D: "ß", 0x2E: "´", 0x2F: "ü", 0x30: "+", 0x32: "#", 0x33: "ö",
             0x34: "ä", 0x36: ",", 0x37: ".", 0x38: "-", 0x64: "<", 0x1C: "z", 0x1D: "y"}
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    LABELS_US[0x04 + _i] = _c
    LABELS_DE.setdefault(0x04 + _i, _c)
for _i, _c in enumerate("1234567890"):
    LABELS_US[0x1E + _i] = LABELS_DE[0x1E + _i] = _c


def _letters(keys):
    return [("key", k, 1) for k in keys]


def keyboard_rows(layout):
    """The rows as (kind, value, width in key units); kind "key" (a HID usage), "mod" (a
    modifier bit), "special" (a usage with a fixed label) or "gap". 16 units per row:
    15 for the main block and one column on the right, as on a tenkeyless keyboard."""
    letters = "qwertyuiop", "asdfghjkl", "zxcvbnm"
    usage = {c: 0x04 + i for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")}
    top = ([("special", KEY_ESC, 1)] + [("fkey", KEY_F1 + i, 1) for i in range(12)]
           + [("special", KEY_PAGEUP, 1), ("special", KEY_PAGEDOWN, 1),
              ("special", KEY_DELETE, 1)])
    numbers = _letters([0x35] + list(range(0x1E, 0x28)) + [0x2D, 0x2E])
    bottom = [("mod", MOD_CTRL, 1.25), ("mod", MOD_SUPER, 1.25), ("mod", MOD_ALT, 1.25),
              ("special", KEY_SPACE, 5.75), ("mod", MOD_RALT, 1.25), ("mod", MOD_RCTRL, 2.25),
              ("special", KEY_LEFT, 1), ("special", KEY_DOWN, 1), ("special", KEY_RIGHT, 1)]
    if layout == "de":
        # ISO: Enter over two rows (drawn as two keys), # left of it, < left of Y
        return [top,
                numbers + [("special", KEY_BACKSPACE, 2), ("special", KEY_HOME, 1)],
                [("special", KEY_TAB, 1.5)] + _letters([usage[c] for c in letters[0]])
                + _letters([0x2F, 0x30]) + [("special", KEY_ENTER, 1.5), ("special", KEY_END, 1)],
                [("special", KEY_CAPS, 1.75)] + _letters([usage[c] for c in letters[1]])
                + _letters([0x33, 0x34, 0x32]) + [("special", KEY_ENTER, 1.25), ("gap", 0, 1)],
                [("mod", MOD_SHIFT, 1.25)] + _letters([0x64] + [usage[c] for c in letters[2]])
                + _letters([0x36, 0x37, 0x38]) + [("mod", 0x20, 1.75), ("special", KEY_UP, 1),
                                                  ("gap", 0, 1)],
                bottom]
    return [top,
            numbers + [("special", KEY_BACKSPACE, 2), ("special", KEY_HOME, 1)],
            [("special", KEY_TAB, 1.5)] + _letters([usage[c] for c in letters[0]])
            + _letters([0x2F, 0x30]) + [("key", 0x31, 1.5), ("special", KEY_END, 1)],
            [("special", KEY_CAPS, 1.75)] + _letters([usage[c] for c in letters[1]])
            + _letters([0x33, 0x34]) + [("special", KEY_ENTER, 2.25), ("gap", 0, 1)],
            [("mod", MOD_SHIFT, 2.25)] + _letters([usage[c] for c in letters[2]])
            + _letters([0x36, 0x37, 0x38]) + [("mod", 0x20, 1.75), ("special", KEY_UP, 1),
                                              ("gap", 0, 1)],
            bottom]


def default_layout():
    """Labels after the phone's own first keyboard layout: German or else US."""
    source = Gio.SettingsSchemaSource.get_default()
    if source and source.lookup("org.gnome.desktop.input-sources", True):
        sources = Gio.Settings(schema_id="org.gnome.desktop.input-sources").get_value("sources")
        if len(sources) and sources[0][1].split("+")[0] == "de":
            return "de"
    return "us"


# ----- Portrait keyboard (laid out like the phone's own keyboard, Stevia) -----

KEY_INSERT = 0x49


def char_strokes(layout):
    """What to press for each character with the host's layout: {char: [(usage, modifiers)]}.

    HID sends key positions, so a character is the key the host's layout puts it on, maybe
    with Shift or AltGr. German accents are a dead key followed by the letter; a dead key on
    its own is followed by a space. Characters the layout cannot type are missing."""
    usage = {c: 0x04 + i for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")}
    strokes = {" ": [(KEY_SPACE, 0)]}
    for i, c in enumerate("1234567890"):
        strokes[c] = [(0x1E + i, 0)]
    if layout == "de":
        usage.update({"y": 0x1D, "z": 0x1C, "ä": 0x34, "ö": 0x33, "ü": 0x2F})
        plain = {"ß": 0x2D, "+": 0x30, "#": 0x32, ",": 0x36, ".": 0x37, "-": 0x38, "<": 0x64}
        shifted = dict(zip('!"§$%&/()=', range(0x1E, 0x28)))
        shifted.update({"?": 0x2D, "*": 0x30, "'": 0x32, "°": 0x35, ";": 0x36, ":": 0x37,
                        "_": 0x38, ">": 0x64})
        altgr = {"@": 0x14, "€": 0x08, "{": 0x24, "[": 0x25, "]": 0x26, "}": 0x27,
                 "\\": 0x2D, "~": 0x30, "|": 0x64, "µ": 0x10, "²": 0x1F, "³": 0x20}
        dead = {"´": (0x2E, 0), "`": (0x2E, MOD_SHIFT), "^": (0x35, 0)}
    else:
        plain = {"-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31, ";": 0x33,
                 "'": 0x34, "`": 0x35, ",": 0x36, ".": 0x37, "/": 0x38}
        shifted = dict(zip("!@#$%^&*()", range(0x1E, 0x28)))
        shifted.update(dict(zip('_+{}|:"~<>?', (0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x33, 0x34,
                                                 0x35, 0x36, 0x37, 0x38))))
        altgr, dead = {}, {}
    for c, u in usage.items():
        strokes[c] = [(u, 0)]
        strokes[c.upper()] = [(u, MOD_SHIFT)]
    for table, mods in ((plain, 0), (shifted, MOD_SHIFT), (altgr, MOD_RALT)):
        for c, u in table.items():
            strokes[c] = [(u, mods)]
    for c, stroke in dead.items():
        strokes[c] = [stroke, (KEY_SPACE, 0)]
    for d, accented in (("´", "áéíóú"), ("`", "àèìòù"), ("^", "âêîôû")):
        if d in dead:
            for base, acc in zip("aeiou", accented):
                strokes[acc] = [dead[d]] + strokes[base]
                strokes[acc.upper()] = [dead[d]] + strokes[base.upper()]
    return strokes


# Long press on these keys offers more characters, as on the phone's keyboard; only those the
# host's layout can type are shown
ALTERNATES = {"a": "äâàá", "e": "éèê", "o": "öôòó", "u": "üûùú", "s": "ß",
              ".": "#!?-:'@"}


def phone_layers(layout):
    """The character rows of the portrait keyboard's three layers, as on the phone: letters,
    numbers and symbols ("123"), more symbols ("={<"). The keys around them (Shift, Backspace,
    layer switches, space, Enter) are added when the layer is built. The second symbol layer
    has the navigation keys in its middle row, the phone's keyboard does not need them."""
    de = layout == "de"
    letters = ["qwertzuiop" if de else "qwertyuiop", "asdfghjkl", "yxcvbnm" if de else "zxcvbnm"]
    symbols = ["1234567890", "@#" + ("€" if de else "$") + "%&-+()", "*\"':;!?"]
    symbols2 = ["~`|\\^={}[]", None, "<>°§µ²³" if de else "<>"]
    return {"letters": (letters, ""), "symbols": (symbols, "_/"), "symbols2": (symbols2, "")}


# Consumer page usages for the media row
CC_PLAY_PAUSE, CC_MUTE, CC_VOL_UP, CC_VOL_DOWN = 0xCD, 0xE2, 0xE9, 0xEA
CC_NEXT, CC_PREVIOUS = 0xB5, 0xB6
CC_AC_HOME, CC_AC_BACK = 0x223, 0x224

# ----- The desktop on a TV (--desktop) -----

SCHWUPP_BUS = "de.cais.Schwupp"
SCHWUPP_PATH = "/de/cais/Schwupp/Cast"
SCHWUPP_IFACE = "de.cais.Schwupp.Cast1"
UINPUT_NAME = "Plinius Remote Input"
# The TV's screen lies right of the phone's: the pointer goes there, to the middle of it, a
# moment after the window is up (phoc puts a new window on the screen with the pointer)
POINTER_TO_TV = (100000, -100000, -960, 540)
POINTER_DELAY_MS = 500
# --desktop runs as a layer surface on the phone's screen that never takes the keyboard focus:
# typing reaches the window on the TV, not this app. gtk4-layer-shell has to load before
# libwayland, so the app starts itself again with it preloaded.
LAYER_SHELL_LIB = "/usr/lib/libgtk4-layer-shell.so"
DESKTOP_APP_ID = APP_ID + ".Desktop"

# HID keyboard usage -> Linux key code, as the kernel maps a real keyboard (hid-input.c)
HID_TO_EVDEV = (
    0, 0, 0, 0, 30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38,
    50, 49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 21, 44, 2, 3,
    4, 5, 6, 7, 8, 9, 10, 11, 28, 1, 14, 15, 57, 12, 13, 26,
    27, 43, 43, 39, 40, 41, 51, 52, 53, 58, 59, 60, 61, 62, 63, 64,
    65, 66, 67, 68, 87, 88, 99, 70, 119, 110, 102, 104, 111, 107, 109, 106,
    105, 108, 103, 69, 98, 55, 74, 78, 96, 79, 80, 81, 75, 76, 77, 71,
    72, 73, 82, 83, 86, 127, 116, 117, 183, 184, 185, 186, 187, 188, 189, 190,
    191, 192, 193, 194, 134, 138, 130, 132, 128, 129, 131, 137, 133, 135, 136, 113,
    115, 114, 0, 0, 0, 121, 0, 89, 93, 124, 92, 94, 95, 0, 0, 0,
    122, 123, 90, 91, 85, 0, 0, 0, 0, 0, 0, 0, 111, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 179, 180, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 111, 0, 0, 0, 0, 0, 0, 0,
    29, 42, 56, 125, 97, 54, 100, 126, 164, 166, 165, 163, 161, 115, 114, 113,
    150, 158, 159, 128, 136, 177, 178, 176, 142, 152, 173, 140, 0, 0, 0, 0,
)

# Modifier byte of the keyboard report, bit by bit: the usages 0xE0..0xE7
MODIFIER_USAGES = tuple(range(0xE0, 0xE8))
# Mouse buttons of the report, bit by bit
MOUSE_BUTTONS = ("BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE", "BTN_SIDE", "BTN_EXTRA")
# Consumer usages of the media row -> Linux key names
CONSUMER_KEYS = {0xCD: "KEY_PLAYPAUSE", 0xE2: "KEY_MUTE", 0xE9: "KEY_VOLUMEUP",
                 0xEA: "KEY_VOLUMEDOWN", 0xB5: "KEY_NEXTSONG", 0xB6: "KEY_PREVIOUSSONG",
                 0x223: "KEY_HOMEPAGE", 0x224: "KEY_BACK"}


class LocalInput:
    """The HID reports as a virtual mouse and keyboard of this phone (uinput).

    Takes the same reports that go to a Bluetooth host, so the touchpad and keyboard behave
    the same; the compositor sees one more mouse and keyboard, as with a Bluetooth one.
    """

    def __init__(self, evdev=None):
        if evdev is None:
            import evdev
        self.ecodes = e = evdev.ecodes
        keys = sorted({k for k in HID_TO_EVDEV if k}
                      | {getattr(e, name) for name in MOUSE_BUTTONS}
                      | {getattr(e, name) for name in CONSUMER_KEYS.values()})
        self.device = evdev.UInput({e.EV_KEY: keys,
                                    e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL]},
                                   name=UINPUT_NAME)
        self.keys = set()            # Linux key codes held now
        self.buttons = 0

    def write(self, kind, code, value):
        self.device.write(kind, code, value)

    def send(self, rid, data):
        e = self.ecodes
        if rid == RID_KEYBOARD:
            held = {HID_TO_EVDEV[u] for bit, u in enumerate(MODIFIER_USAGES) if data[0] >> bit & 1}
            held |= {HID_TO_EVDEV[u] for u in data[2:8] if u and HID_TO_EVDEV[u]}
            # Modifiers go down first and up last, as typed on a keyboard: Shift before "a"
            mods = {HID_TO_EVDEV[u] for u in MODIFIER_USAGES}
            order = lambda code: (code in mods, code)  # noqa: E731
            for code in sorted(self.keys - held, key=order):
                self.write(e.EV_KEY, code, 0)
            for code in sorted(held - self.keys, key=lambda c: (c not in mods, c)):
                self.write(e.EV_KEY, code, 1)
            self.keys = held
        elif rid == RID_MOUSE:
            for bit, name in enumerate(MOUSE_BUTTONS):
                if (data[0] ^ self.buttons) >> bit & 1:
                    self.write(e.EV_KEY, getattr(e, name), data[0] >> bit & 1)
            self.buttons = data[0]
            for code, value in ((e.REL_X, int.from_bytes(data[1:3], "little", signed=True)),
                                (e.REL_Y, int.from_bytes(data[3:5], "little", signed=True)),
                                (e.REL_WHEEL, int.from_bytes(data[5:6], "little", signed=True)),
                                (e.REL_HWHEEL, int.from_bytes(data[6:7], "little",
                                                              signed=True))):
                if value:
                    self.write(e.EV_REL, code, value)
        elif rid == RID_CONSUMER:
            name = CONSUMER_KEYS.get(int.from_bytes(data[:2], "little"))
            if name is None:
                return           # the release, or a key without a Linux counterpart
            self.write(e.EV_KEY, getattr(e, name), 1)
            self.device.syn()
            self.write(e.EV_KEY, getattr(e, name), 0)
        self.device.syn()

    def pointer_to_tv(self):
        """Move the pointer onto the TV: against the far edges, then back to the middle."""
        e = self.ecodes
        far_x, far_y, back_x, back_y = POINTER_TO_TV
        for dx, dy in ((far_x, far_y), (back_x, back_y)):
            self.write(e.EV_REL, e.REL_X, dx)
            self.write(e.EV_REL, e.REL_Y, dy)
            self.device.syn()

    def close(self):
        for code in sorted(self.keys):
            self.write(self.ecodes.EV_KEY, code, 0)
        self.keys = set()
        self.device.syn()
        self.device.close()


# ----- Touchpad feel -----

TAP_MS = 300             # a touch shorter than this ...
TAP_SLOP = 12.0          # ... that moved less than this many pixels is a click
LONG_PRESS_MS = 450      # holding still this long grabs the left button for dragging
ACCEL_THRESHOLD = 0.25   # px/ms; slower movement is passed on 1:1 times the speed setting
ACCEL_GAIN = 1.6         # how much faster movement is amplified above the threshold
ACCEL_CAP = 2.5          # px/ms above the threshold where amplification stops growing
SCROLL_STEP = 22.0       # pixels of two-finger movement per wheel detent
DOUBLE_TAP_MS = 400      # touching again this soon after a tap holds the button (tap and drag)
SWIPE_STEP = 90.0        # pixels of three-finger movement per window switch
ZOOM_STEP = 0.12         # change of the pinch's spread per zoom step (Ctrl + wheel)
REPORT_INTERVAL_MS = 15  # at most one report per typical BLE connection interval

# ----- D-Bus interfaces we export -----

NODE_XML = """
<node>
  <interface name="org.freedesktop.DBus.ObjectManager">
    <method name="GetManagedObjects">
      <arg name="objects" type="a{oa{sa{sv}}}" direction="out"/>
    </method>
  </interface>
  <interface name="org.bluez.GattService1">
    <property name="UUID" type="s" access="read"/>
    <property name="Primary" type="b" access="read"/>
  </interface>
  <interface name="org.bluez.GattCharacteristic1">
    <method name="ReadValue">
      <arg name="options" type="a{sv}" direction="in"/>
      <arg name="value" type="ay" direction="out"/>
    </method>
    <method name="WriteValue">
      <arg name="value" type="ay" direction="in"/>
      <arg name="options" type="a{sv}" direction="in"/>
    </method>
    <method name="StartNotify"/>
    <method name="StopNotify"/>
    <property name="UUID" type="s" access="read"/>
    <property name="Service" type="o" access="read"/>
    <property name="Flags" type="as" access="read"/>
    <property name="Notifying" type="b" access="read"/>
    <property name="Value" type="ay" access="read"/>
  </interface>
  <interface name="org.bluez.GattDescriptor1">
    <method name="ReadValue">
      <arg name="options" type="a{sv}" direction="in"/>
      <arg name="value" type="ay" direction="out"/>
    </method>
    <method name="WriteValue">
      <arg name="value" type="ay" direction="in"/>
      <arg name="options" type="a{sv}" direction="in"/>
    </method>
    <property name="UUID" type="s" access="read"/>
    <property name="Characteristic" type="o" access="read"/>
    <property name="Flags" type="as" access="read"/>
  </interface>
  <interface name="org.bluez.LEAdvertisement1">
    <method name="Release"/>
    <property name="Type" type="s" access="read"/>
    <property name="ServiceUUIDs" type="as" access="read"/>
    <property name="Appearance" type="q" access="read"/>
    <property name="LocalName" type="s" access="read"/>
    <property name="Discoverable" type="b" access="read"/>
    <property name="MinInterval" type="u" access="read"/>
    <property name="MaxInterval" type="u" access="read"/>
  </interface>
  <interface name="org.bluez.Agent1">
    <method name="Release"/>
    <method name="RequestPinCode">
      <arg name="device" type="o" direction="in"/>
      <arg name="pincode" type="s" direction="out"/>
    </method>
    <method name="DisplayPinCode">
      <arg name="device" type="o" direction="in"/>
      <arg name="pincode" type="s" direction="in"/>
    </method>
    <method name="RequestPasskey">
      <arg name="device" type="o" direction="in"/>
      <arg name="passkey" type="u" direction="out"/>
    </method>
    <method name="DisplayPasskey">
      <arg name="device" type="o" direction="in"/>
      <arg name="passkey" type="u" direction="in"/>
      <arg name="entered" type="q" direction="in"/>
    </method>
    <method name="RequestConfirmation">
      <arg name="device" type="o" direction="in"/>
      <arg name="passkey" type="u" direction="in"/>
    </method>
    <method name="RequestAuthorization">
      <arg name="device" type="o" direction="in"/>
    </method>
    <method name="AuthorizeService">
      <arg name="device" type="o" direction="in"/>
      <arg name="uuid" type="s" direction="in"/>
    </method>
    <method name="Cancel"/>
  </interface>
</node>
"""
INTERFACES = {i.name: i for i in Gio.DBusNodeInfo.new_for_xml(NODE_XML).interfaces}


def log(text):
    """Log lines go to stderr, which the session hands to the journal."""
    print(f"plinius-remote: {text}", file=sys.stderr, flush=True)


def error_text(err):
    """A GLib.Error from a D-Bus call without the "GDBus.Error:org.bluez.Error.X: " prefix."""
    message = err.message
    if message.startswith("GDBus.Error:") and ": " in message:
        message = message.split(": ", 1)[1]
    return message


def battery_level():
    """The phone's charge in percent for the Battery Service; 100 if it cannot be read."""
    try:
        with open(BATTERY) as f:
            return max(0, min(100, int(f.read().strip())))
    except (OSError, ValueError):
        return 100


# ----- GATT objects -----

class Service:
    def __init__(self, path, uuid):
        self.path, self.uuid, self.characteristics = path, uuid, []

    def properties(self):
        return {SERVICE_IFACE: {"UUID": GLib.Variant("s", self.uuid),
                                "Primary": GLib.Variant("b", True)}}


class Characteristic:
    """A characteristic with a stored value; on_write(value) sees what the host writes."""

    def __init__(self, service, uuid, flags, value=b"", on_write=None):
        self.service = service
        self.path = f"{service.path}/char{len(service.characteristics)}"
        self.uuid, self.flags, self.value, self.on_write = uuid, flags, bytes(value), on_write
        self.notifying = False
        self.descriptors = []
        service.characteristics.append(self)

    def properties(self):
        return {CHRC_IFACE: {"UUID": GLib.Variant("s", self.uuid),
                             "Service": GLib.Variant("o", self.service.path),
                             "Flags": GLib.Variant("as", self.flags),
                             "Notifying": GLib.Variant("b", self.notifying),
                             "Value": GLib.Variant("ay", self.value)}}


class Descriptor:
    def __init__(self, chrc, uuid, flags, value):
        self.chrc = chrc
        self.path = f"{chrc.path}/desc{len(chrc.descriptors)}"
        self.uuid, self.flags, self.value = uuid, flags, bytes(value)
        chrc.descriptors.append(self)

    def properties(self):
        return {DESC_IFACE: {"UUID": GLib.Variant("s", self.uuid),
                             "Characteristic": GLib.Variant("o", self.chrc.path),
                             "Flags": GLib.Variant("as", self.flags)}}


# ----- The BlueZ side: GATT application, advertisement, agent -----

class Peripheral:
    """Exports the HID device to BlueZ and keeps track of the host that uses it.

    The UI hands in callbacks: changed() whenever state, name or host change, error(text) for
    failures worth a toast, and pair(kind, name, passkey, answer) / pair_cancel() for the
    agent's questions. answer(bool) may be called later; the agent's D-Bus call waits for it.

    known_hosts is a set of device object paths that used us before; new_host(path) is called
    when one is added, so the UI can store it. A bonded host that reconnects often just
    re-enables notifications inside BlueZ without touching our objects, so recognising its
    connection is the only reliable sign that it is back.
    """

    OFF, STARTING, VISIBLE, CONNECTED, STOPPING = range(5)

    def __init__(self, changed, error, pair, pair_cancel, known_hosts, new_host):
        self.changed, self.error, self.pair, self.pair_cancel = changed, error, pair, pair_cancel
        self.known_hosts, self.new_host = known_hosts, new_host
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.bluez_owner = None
        self.adapter = None          # object path of the adapter we use, e.g. /org/bluez/hci0
        self.adapter_props = {}
        self.devices = {}            # path -> Device1 properties we care about (Alias, Connected)
        self.state = self.OFF
        self.registrations = []      # ids from register_object, for unexporting
        self.objects = {}            # path -> Service / Characteristic / Descriptor
        self.app_registered = self.agent_registered = False
        self.adv_registered = self.adv_busy = False
        self.adv_method = None       # the (Un)RegisterAdvertisement BlueZ has not answered yet
        self.restore_pairable = False
        self.start_run = None        # token of the running start(), a new one per start
        self.start_step = None       # label of the start step BlueZ has not answered yet
        self.host = None             # device using us: seen in Read/WriteValue or a known host
        self.notify_count = 0        # input reports a host subscribed to
        self.pending_agent = None    # invocation waiting for the user's answer
        self.name = ""
        self.build_gatt()
        self.bus.signal_subscribe(BLUEZ, PROPS_IFACE, "PropertiesChanged", None, None,
                                  Gio.DBusSignalFlags.NONE, self.on_props_changed)
        self.bus.signal_subscribe(BLUEZ, OM_IFACE, None, "/", None,
                                  Gio.DBusSignalFlags.NONE, self.on_objects_changed)
        Gio.bus_watch_name_on_connection(self.bus, BLUEZ, Gio.BusNameWatcherFlags.NONE,
                                         self.on_bluez_appeared, self.on_bluez_vanished)

    # ----- GATT database -----

    def build_gatt(self):
        """The services as hosts expect them from a BLE keyboard/mouse (HOGP 1.0)."""
        hid = Service(APP_PATH + "/hid", uuid16(0x1812))
        # bcdHID 1.11, no country, flags: normally connectable (the host may reconnect)
        Characteristic(hid, uuid16(0x2A4A), ["read"], b"\x11\x01\x00\x02")
        # The report map and the reports need an encrypted link. Hosts answer the
        # "insufficient encryption" error by pairing, which gives them a bond to reconnect with.
        Characteristic(hid, uuid16(0x2A4B), ["encrypt-read"], REPORT_MAP)
        # Control point: suspend/exit suspend. Nothing to do here, a phone does not sleep
        # differently for it.
        Characteristic(hid, uuid16(0x2A4C), ["write-without-response"], b"\x00",
                       on_write=lambda v: log(f"host sent HID control {bytes(v).hex()}"))
        # Protocol mode: report mode (1). There are no boot protocol reports, so a host that
        # switches to boot mode keeps getting report mode reports; all usual hosts stay in
        # report mode.
        self.protocol_mode = Characteristic(hid, uuid16(0x2A4E),
                                            ["read", "write-without-response"], b"\x01")
        self.reports = {}
        for rid, kind in ((RID_KEYBOARD, "input"), (RID_MOUSE, "input"),
                          (RID_CONSUMER, "input"), (RID_KEYBOARD, "output")):
            length = REPORT_LENGTHS[(rid, kind)]
            if kind == "input":
                # encrypt-notify: enabling notifications (the CCC write) needs encryption too
                flags = ["encrypt-read", "notify", "encrypt-notify"]
                chrc = Characteristic(hid, uuid16(0x2A4D), flags, bytes(length))
                self.reports[rid] = chrc
            else:
                # Keyboard LEDs from the host (Caps Lock and friends)
                flags = ["encrypt-read", "write-without-response", "encrypt-write"]
                chrc = Characteristic(hid, uuid16(0x2A4D), flags, bytes(length),
                                      on_write=self.on_leds)
            # Report Reference: Report ID and type (1 input, 2 output)
            Descriptor(chrc, uuid16(0x2908), ["read"], bytes([rid, 1 if kind == "input" else 2]))

        battery = Service(APP_PATH + "/battery", uuid16(0x180F))
        self.battery = Characteristic(battery, uuid16(0x2A19), ["read", "notify"],
                                      bytes([battery_level()]))

        # BlueZ already serves a Device Information service with a PnP ID (from its DeviceID
        # setting). Ours is only exported when the adapter does not list one, see export().
        self.devinfo = Service(APP_PATH + "/devinfo", uuid16(0x180A))
        # PnP ID: vendor ID source USB-IF (2), Linux Foundation (0x1D6B), product 0x0246
        # (BlueZ), version 1.0
        Characteristic(self.devinfo, uuid16(0x2A50), ["read"], b"\x02\x6b\x1d\x46\x02\x00\x01")

        self.services = [hid, battery, self.devinfo]

    def on_leds(self, value):
        if value:
            leds = value[0]
            log(f"host LEDs: num {leds & 1}, caps {leds >> 1 & 1}, scroll {leds >> 2 & 1}")

    def notify(self, rid, data):
        """Send an input report. BlueZ forwards a changed Value to every subscribed host."""
        chrc = self.reports[rid]
        chrc.value = bytes(data)
        if self.state in (self.VISIBLE, self.CONNECTED):
            self.emit_value(chrc)

    def emit_value(self, chrc):
        self.bus.emit_signal(None, chrc.path, PROPS_IFACE, "PropertiesChanged",
                             GLib.Variant("(sa{sv}as)", (CHRC_IFACE, {
                                 "Value": GLib.Variant("ay", chrc.value)}, [])))

    def update_battery(self):
        level = bytes([battery_level()])
        if level != self.battery.value:
            self.battery.value = level
            if self.state in (self.VISIBLE, self.CONNECTED):
                self.emit_value(self.battery)

    # ----- D-Bus helpers -----

    def call(self, path, iface, method, params=None, reply="()", done=None):
        """Asynchronous call to BlueZ; done(result_or_None, error_or_None).

        Must be asynchronous: BlueZ calls back into our objects (GetManagedObjects, property
        reads) before RegisterApplication returns, and those calls need a running main loop.
        """
        def finish(conn, res):
            try:
                result = conn.call_finish(res).unpack()
            except GLib.Error as e:
                if done:
                    done(None, e)
                return
            if done:
                done(result, None)
        self.bus.call(BLUEZ, path, iface, method, params, GLib.VariantType(reply),
                      Gio.DBusCallFlags.NONE, 25000, None, finish)

    def run_steps(self, steps, finished):
        """Run [(label, path, iface, method, params, fatal)] one after the other.

        finished(error_text_or_None) is called at the end or at the first fatal error.
        """
        if not steps:
            finished(None)
            return
        label, path, iface, method, params, fatal = steps[0]

        def done(_res, err):
            if err:
                log(f"{label} failed: {err.message}")
                if fatal:
                    finished(f"{label}: {error_text(err)}")
                    return
            self.run_steps(steps[1:], finished)
        self.call(path, iface, method, params, done=done)

    def set_adapter_prop(self, name, sig, value):
        return (f"Set {name}", self.adapter, PROPS_IFACE, "Set",
                GLib.Variant("(ssv)", (ADAPTER_IFACE, name, GLib.Variant(sig, value))), True)

    # ----- BlueZ appearing and going -----

    def on_bluez_appeared(self, _conn, _name, owner):
        self.bluez_owner = owner
        self.call("/", OM_IFACE, "GetManagedObjects", reply="(a{oa{sa{sv}}})",
                  done=self.on_bluez_objects)

    def on_bluez_objects(self, res, err):
        if err:
            log(f"reading BlueZ objects failed: {err.message}")
            return
        for path, ifaces in sorted(res[0].items()):
            self.add_object(path, ifaces)
        self.changed()

    def add_object(self, path, ifaces):
        if (ADAPTER_IFACE in ifaces and GATT_MANAGER_IFACE in ifaces
                and ADV_MANAGER_IFACE in ifaces and self.adapter is None):
            self.adapter = path
            self.adapter_props = dict(ifaces[ADAPTER_IFACE])
            log(f"using adapter {path} ({self.adapter_props.get('Address')})")
        if DEVICE_IFACE in ifaces:
            props = ifaces[DEVICE_IFACE]
            self.devices[path] = {"Alias": props.get("Alias", props.get("Address", "")),
                                  "Connected": props.get("Connected", False)}

    def on_bluez_vanished(self, _conn, _name):
        # bluetoothd is gone (restart, crash): everything we registered went with it
        if self.bluez_owner:
            log("BlueZ left the bus")
        self.bluez_owner = None
        self.adapter, self.adapter_props, self.devices = None, {}, {}
        self.app_registered = self.agent_registered = self.adv_registered = False
        self.adv_busy, self.adv_method = False, None
        if self.state != self.OFF:
            self.unexport()
            self.set_state(self.OFF)
        self.changed()

    def on_objects_changed(self, _conn, _sender, _path, _iface, signal_name, params):
        if signal_name == "InterfacesAdded":
            path, ifaces = params.unpack()
            self.add_object(path, ifaces)
            self.changed()
        elif signal_name == "InterfacesRemoved":
            path, ifaces = params.unpack()
            if ADAPTER_IFACE in ifaces and path == self.adapter:
                self.on_bluez_vanished(None, None)
            self.devices.pop(path, None)

    def on_props_changed(self, _conn, _sender, path, _iface, _signal, params):
        iface, changed, _invalid = params.unpack()
        if iface == ADAPTER_IFACE and path == self.adapter:
            self.adapter_props.update(changed)
            if "Powered" in changed and not changed["Powered"] and self.state != self.OFF:
                self.error(_("Bluetooth was switched off"))
                self.stop()
            self.changed()
        elif iface == DEVICE_IFACE and path in self.devices:
            dev = self.devices[path]
            for key in ("Alias", "Connected"):
                if key in changed:
                    dev[key] = changed[key]
            if "Connected" in changed:
                self.on_device_connected(path, changed["Connected"])

    def on_device_connected(self, path, connected):
        if connected:
            log(f"{path} connected")
            if path in self.known_hosts and self.host is None:
                self.host = path
        else:
            log(f"{path} disconnected")
            if path == self.host:
                # BlueZ normally calls StopNotify on its own; do not rely on it for the state
                self.host = None
                self.notify_count = 0
                for chrc in self.reports.values():
                    chrc.notifying = False
                if self.state == self.CONNECTED:
                    self.set_state(self.VISIBLE)
        self.update_connection()

    def device_name(self, path):
        dev = self.devices.get(path)
        return dev["Alias"] if dev else path.rsplit("/", 1)[-1].replace("dev_", "")

    # ----- Exported objects -----

    def export(self):
        """Put the GATT tree, the advertisement and the agent on the bus."""
        uuids = self.adapter_props.get("UUIDs", [])
        services = [s for s in self.services
                    if s is not self.devinfo or uuid16(0x180A) not in uuids]
        self.objects = {}
        for service in services:
            self.objects[service.path] = service
            for chrc in service.characteristics:
                self.objects[chrc.path] = chrc
                for desc in chrc.descriptors:
                    self.objects[desc.path] = desc
        reg = self.bus.register_object_with_closures2
        self.registrations = [reg(APP_PATH, INTERFACES[OM_IFACE], self.on_method, None, None),
                              reg(ADV_PATH, INTERFACES[ADV_IFACE], self.on_method,
                                  self.on_get_property, None),
                              reg(AGENT_PATH, INTERFACES[AGENT_IFACE], self.on_method, None, None)]
        for path, obj in self.objects.items():
            iface = {Service: SERVICE_IFACE, Characteristic: CHRC_IFACE,
                     Descriptor: DESC_IFACE}[type(obj)]
            self.registrations.append(reg(path, INTERFACES[iface], self.on_method,
                                          self.on_get_property, None))

    def unexport(self):
        for reg_id in self.registrations:
            self.bus.unregister_object(reg_id)
        self.registrations = []

    def advertisement_properties(self):
        return {"Type": GLib.Variant("s", "peripheral"),
                "ServiceUUIDs": GLib.Variant("as", [uuid16(0x1812)]),
                "Appearance": GLib.Variant("q", APPEARANCE_HID),
                "LocalName": GLib.Variant("s", self.name),
                "Discoverable": GLib.Variant("b", True),
                # Default intervals can be over a second; hosts then take long to find us
                "MinInterval": GLib.Variant("u", 100),
                "MaxInterval": GLib.Variant("u", 150)}

    def on_get_property(self, _conn, _sender, path, iface, name):
        if path == ADV_PATH:
            return self.advertisement_properties()[name]
        return self.objects[path].properties()[iface][name]

    def on_method(self, _conn, sender, path, iface, method, params, invocation):
        # Only bluetoothd may use our objects; the bus policy should already ensure that
        if sender != self.bluez_owner:
            invocation.return_dbus_error("org.bluez.Error.NotAuthorized", "Not BlueZ")
            return
        args = params.unpack()
        if iface == OM_IFACE:
            tree = {p: o.properties() for p, o in self.objects.items()}
            invocation.return_value(GLib.Variant("(a{oa{sa{sv}}})", (tree,)))
        elif iface in (CHRC_IFACE, DESC_IFACE):
            self.on_gatt_method(self.objects[path], method, args, invocation)
        elif iface == ADV_IFACE:
            # Release: BlueZ dropped the advertisement on its own (adapter reset, power off)
            log("advertisement released by BlueZ")
            self.adv_registered = False
            invocation.return_value(None)
        elif iface == AGENT_IFACE:
            self.on_agent_method(method, args, invocation)

    def on_gatt_method(self, obj, method, args, invocation):
        if method in ("ReadValue", "WriteValue"):
            options = args[0] if method == "ReadValue" else args[1]
            # The device option names the host; a host that reads or writes HID data is the
            # one we are connected to, even if it connected before we noticed
            device = options.get("device")
            if device and device != self.host:
                self.host = device
                if device not in self.known_hosts:
                    self.known_hosts.add(device)
                    self.new_host(device)
                self.update_connection()
        if method == "ReadValue":
            # Long values (the report map) are read in pieces starting at "offset"
            offset = options.get("offset", 0)
            invocation.return_value(GLib.Variant("(ay)", (obj.value[offset:],)))
        elif method == "WriteValue":
            value, options = bytes(args[0]), args[1]
            offset = options.get("offset", 0)
            obj.value = obj.value[:offset] + value
            if getattr(obj, "on_write", None):
                obj.on_write(obj.value)
            invocation.return_value(None)
        elif method == "StartNotify":
            if not obj.notifying:
                obj.notifying = True
                if obj in self.reports.values():
                    self.notify_count += 1
                    self.update_connection()
            invocation.return_value(None)
        elif method == "StopNotify":
            if obj.notifying:
                obj.notifying = False
                if obj in self.reports.values():
                    self.notify_count = max(0, self.notify_count - 1)
                    self.update_connection()
            invocation.return_value(None)
        else:
            invocation.return_dbus_error("org.bluez.Error.NotSupported", method)

    # ----- Pairing agent -----

    def on_agent_method(self, method, args, invocation):
        if method == "Release":
            self.agent_registered = False
            invocation.return_value(None)
        elif method == "Cancel":
            # The host gave up or the pairing timed out; drop the open question
            if self.pending_agent:
                self.pending_agent = None
                self.pair_cancel()
            invocation.return_value(None)
        elif method in ("RequestPinCode", "RequestPasskey"):
            # We registered as DisplayYesNo: we show numbers, we never type them
            invocation.return_dbus_error("org.bluez.Error.Rejected", "No keyboard on this side")
        elif method in ("DisplayPinCode", "DisplayPasskey"):
            # The host wants the number typed on its side; show it until pairing ends
            self.ask(invocation, "display", args[0], args[1], reply_now=True)
        elif method == "RequestConfirmation":
            self.ask(invocation, "confirm", args[0], args[1])
        elif method == "RequestAuthorization":
            self.ask(invocation, "authorize", args[0], None)
        elif method == "AuthorizeService":
            # While we are the default agent, classic profile connections of other devices
            # come here too. Paired devices are let through, as without an agent.
            path = args[0]
            paired = False
            try:
                paired = self.bus.call_sync(BLUEZ, path, PROPS_IFACE, "Get",
                                            GLib.Variant("(ss)", (DEVICE_IFACE, "Paired")),
                                            GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE,
                                            2000, None).unpack()[0]
            except GLib.Error:
                pass
            if paired:
                invocation.return_value(None)
            else:
                invocation.return_dbus_error("org.bluez.Error.Rejected", "Not paired")
        else:
            invocation.return_dbus_error("org.bluez.Error.Rejected", method)

    def ask(self, invocation, kind, device, passkey, reply_now=False):
        if self.pending_agent:
            # A new question replaces an open one; clear it first so closing its dialog
            # does not answer it a second time
            old, self.pending_agent = self.pending_agent, None
            old.return_dbus_error("org.bluez.Error.Canceled", "Superseded")
            self.pair_cancel()
        self.pending_agent = None if reply_now else invocation
        if reply_now:
            invocation.return_value(None)

        def answer(yes):
            if reply_now or self.pending_agent is not invocation:
                return
            self.pending_agent = None
            if yes:
                log(f"pairing with {device} accepted")
                invocation.return_value(None)
            else:
                log(f"pairing with {device} rejected")
                invocation.return_dbus_error("org.bluez.Error.Rejected", "Rejected by the user")
        code = f"{passkey:06d}" if isinstance(passkey, int) else passkey
        self.pair(kind, self.device_name(device), code, answer)

    # ----- Switching on and off -----

    def available(self):
        return self.adapter is not None

    def start(self):
        if self.state != self.OFF or not self.adapter:
            return
        self.name = self.local_name()
        self.host = next((p for p in self.known_hosts
                          if self.devices.get(p, {}).get("Connected")), None)
        self.notify_count = 0
        for chrc in self.reports.values():
            chrc.notifying = False
        self.set_state(self.STARTING)
        self.export()
        steps = []
        if not self.adapter_props.get("Powered"):
            steps.append(self.set_adapter_prop("Powered", "b", True))
        # Without "pairable" the kernel pairs without bonding, and the host could not
        # reconnect later. The previous value comes back on stop().
        self.restore_pairable = not self.adapter_props.get("Pairable", False)
        if self.restore_pairable:
            steps.append(self.set_adapter_prop("Pairable", "b", True))
        steps += [
            # DisplayYesNo: hosts with a screen get numeric comparison, the user confirms
            # the code here; a nearby stranger cannot pair silently and read what is typed
            ("RegisterAgent", "/org/bluez", AGENT_MANAGER_IFACE, "RegisterAgent",
             GLib.Variant("(os)", (AGENT_PATH, "DisplayYesNo")), True),
            ("RequestDefaultAgent", "/org/bluez", AGENT_MANAGER_IFACE, "RequestDefaultAgent",
             GLib.Variant("(o)", (AGENT_PATH,)), False),
            ("RegisterApplication", self.adapter, GATT_MANAGER_IFACE, "RegisterApplication",
             GLib.Variant("(oa{sv})", (APP_PATH, {})), True),
        ]
        # Track what got registered as we go, so stop() undoes exactly that
        token = self.start_run = object()

        def run(rest):
            if not rest:
                started(None)
                return
            label = self.start_step = rest[0][0]

            def step_done(err):
                if self.start_run is not token or self.state != self.STARTING:
                    # Stopped meanwhile: stop() undoes this step if it registered something,
                    # and the next one must not run
                    return
                self.start_step = None
                if err:
                    started(err)
                    return
                if label in START_MARKS:
                    setattr(self, START_MARKS[label], True)
                run(rest[1:])
            self.run_steps(rest[:1], step_done)

        def started(err):  # only while starting: step_done drops a stopped start's answers
            if err:
                self.error(_("Could not start: {error}").format(error=err))
                self.stop()
                return
            log(f"GATT application registered, advertising as “{self.name}”")
            self.set_state(self.VISIBLE)
            self.sync_advertising()
        run(steps)

    def local_name(self):
        """'<phone name> Remote', short enough for a legacy advertisement."""
        base = self.adapter_props.get("Alias") or GLib.get_host_name() or "Plinius"
        while base and len(f"{base} Remote".encode()) > MAX_NAME_BYTES:
            base = base[:-1]
        return f"{base.strip() or 'Plinius'} Remote"

    def stop(self, sync=False):
        """Undo start(). sync=True is for quitting, when no main loop runs any more."""
        if self.state == self.OFF:
            return
        if self.pending_agent:
            old, self.pending_agent = self.pending_agent, None
            old.return_dbus_error("org.bluez.Error.Canceled", "Stopped")
            self.pair_cancel()
        if self.state == self.STARTING and self.start_step in START_MARKS:
            # BlueZ has the call but did not answer yet; the unregister goes after it on the
            # bus, so BlueZ handles both in this order (a failed register makes the
            # unregister fail too, which is only logged)
            setattr(self, START_MARKS[self.start_step], True)
        self.start_run = self.start_step = None
        steps = []
        # An advertisement call still on its way counts as answered: the unregister goes after it
        if (self.adv_method == "RegisterAdvertisement" if self.adv_busy
                else self.adv_registered):
            steps.append(("UnregisterAdvertisement", self.adapter, ADV_MANAGER_IFACE,
                          "UnregisterAdvertisement", GLib.Variant("(o)", (ADV_PATH,)), False))
        if self.app_registered:
            steps.append(("UnregisterApplication", self.adapter, GATT_MANAGER_IFACE,
                          "UnregisterApplication", GLib.Variant("(o)", (APP_PATH,)), False))
        if self.agent_registered:
            steps.append(("UnregisterAgent", "/org/bluez", AGENT_MANAGER_IFACE,
                          "UnregisterAgent", GLib.Variant("(o)", (AGENT_PATH,)), False))
        if self.restore_pairable and self.adapter:
            steps.append(("Set Pairable", self.adapter, PROPS_IFACE, "Set",
                          GLib.Variant("(ssv)", (ADAPTER_IFACE, "Pairable",
                                                 GLib.Variant("b", False))), False))
        self.adv_registered = self.app_registered = self.agent_registered = False
        self.restore_pairable = False
        self.host = None
        self.notify_count = 0
        log("stopping")
        if sync or not self.bluez_owner:
            # Leaving: BlueZ would drop our objects anyway once we are off the bus, but the
            # Pairable setting stays unless we put it back
            for label, path, iface, method, params, _fatal in steps if self.bluez_owner else []:
                try:
                    self.bus.call_sync(BLUEZ, path, iface, method, params, None,
                                       Gio.DBusCallFlags.NONE, 2000, None)
                except GLib.Error as e:
                    log(f"{label} failed: {e.message}")
            self.unexport()
            self.set_state(self.OFF)
            return
        self.set_state(self.STOPPING)

        def stopped(_err):
            self.unexport()
            self.set_state(self.OFF)
        self.run_steps(steps, stopped)

    def set_state(self, state):
        self.state = state
        self.changed()

    # ----- Advertising follows the connection -----

    def update_connection(self):
        """Connected while a host subscribed to reports or a known host is connected."""
        if self.state not in (self.VISIBLE, self.CONNECTED):
            return
        host_up = any(self.devices.get(p, {}).get("Connected") for p in (self.host,) if p)
        connected = self.notify_count > 0 or host_up
        new = self.CONNECTED if connected else self.VISIBLE
        if new != self.state:
            log("host connected" if connected else "host gone")
            self.set_state(new)
        else:
            self.changed()
        self.sync_advertising()

    def sync_advertising(self):
        """Advertise while visible and nobody uses us, so no second host connects meanwhile;
        a bonded host finds us again through the same advertisement after a disconnect."""
        if self.adv_busy or not self.adapter:
            return
        want = self.state == self.VISIBLE
        if want == self.adv_registered:
            return
        self.adv_busy = True
        method = self.adv_method = "RegisterAdvertisement" if want else "UnregisterAdvertisement"
        params = (GLib.Variant("(oa{sv})", (ADV_PATH, {})) if want
                  else GLib.Variant("(o)", (ADV_PATH,)))

        def done(_res, err):
            self.adv_busy = False
            self.adv_method = None
            if err:
                log(f"{method} failed: {err.message}")
            if self.state not in (self.VISIBLE, self.CONNECTED):
                return  # stopped meanwhile: stop() took care of the advertisement
            if err:
                if want:
                    self.error(_("Could not become visible: {error}").format(
                        error=error_text(err)))
                    self.stop()
                    return
            else:
                self.adv_registered = want
            self.sync_advertising()
        self.call(self.adapter, ADV_MANAGER_IFACE, method, params, done=done)

    def host_name(self):
        return self.device_name(self.host) if self.host else None


# ----- Reports: turning touches and keys into HID reports at a steady pace -----

class Reports:
    """Queues key and button reports and coalesces pointer motion between them.

    BLE delivers at most a few notifications per connection interval; sending every touch
    event would pile up in the kernel's queue and make the pointer lag behind the finger.
    So motion and wheel are summed up and sent at most every REPORT_INTERVAL_MS, while key
    presses and button changes keep their order in a queue that is drained first.
    """

    def __init__(self, send):
        self.send = send             # send(report_id, bytes)
        self.queue = []
        self.buttons = 0
        self.dx = self.dy = self.wheel = self.pan = 0.0
        self.timer = 0

    def kick(self):
        """Send right away if the last report is long enough ago, else on the timer."""
        if not self.timer:
            self.tick()
            # The timer also spaces out the next report after one sent right away
            self.timer = GLib.timeout_add(REPORT_INTERVAL_MS, self.on_timer)

    def on_timer(self):
        if not (self.queue or self.pending_motion()):
            self.timer = 0
            return False
        self.tick()
        return True

    def tick(self):
        if self.queue:
            self.send(*self.queue.pop(0))
        elif self.pending_motion():
            self.send(RID_MOUSE, self.take_motion())

    def pending_motion(self):
        return (abs(self.dx) >= 1 or abs(self.dy) >= 1 or abs(self.wheel) >= 1
                or abs(self.pan) >= 1)

    def take_motion(self):
        """The mouse report with the motion summed up so far; fractions stay for later."""
        x = max(-32767, min(32767, int(self.dx)))
        y = max(-32767, min(32767, int(self.dy)))
        w = max(-127, min(127, int(self.wheel)))
        p = max(-127, min(127, int(self.pan)))
        self.dx, self.dy, self.wheel, self.pan = self.dx - x, self.dy - y, self.wheel - w, \
            self.pan - p
        return (bytes([self.buttons]) + x.to_bytes(2, "little", signed=True)
                + y.to_bytes(2, "little", signed=True)
                + w.to_bytes(1, "little", signed=True) + p.to_bytes(1, "little", signed=True))

    def move(self, dx, dy):
        self.dx += dx
        self.dy += dy
        self.kick()

    def scroll(self, wheel, pan):
        self.wheel += wheel
        self.pan += pan
        self.kick()

    def set_buttons(self, buttons):
        # Motion collected so far goes out with the old buttons, then the change
        if self.pending_motion():
            self.queue.append((RID_MOUSE, self.take_motion()))
        self.buttons = buttons
        self.queue.append((RID_MOUSE, bytes([buttons]) + bytes(6)))
        self.kick()

    def click(self, button):
        self.set_buttons(self.buttons | button)
        self.set_buttons(self.buttons & ~button)

    def key(self, modifiers, usage):
        """Press and release one key with modifiers held."""
        self.queue.append((RID_KEYBOARD, bytes([modifiers, 0, usage, 0, 0, 0, 0, 0])))
        self.queue.append((RID_KEYBOARD, bytes(8)))
        self.kick()

    def keys(self, modifiers, usages):
        """The keyboard as it is now: modifiers and up to six keys held. The host repeats a
        held key itself, as with a real keyboard."""
        held = list(usages)[:6]
        self.queue.append((RID_KEYBOARD, bytes([modifiers, 0] + held + [0] * (6 - len(held)))))
        self.kick()

    def zoom(self, steps):
        """Ctrl held around wheel steps: zoom in (positive) or out on the host."""
        if self.pending_motion():
            self.queue.append((RID_MOUSE, self.take_motion()))
        steps = max(-127, min(127, steps))
        self.queue.append((RID_KEYBOARD, bytes([MOD_CTRL]) + bytes(7)))
        self.queue.append((RID_MOUSE, bytes([self.buttons]) + bytes(4)
                           + steps.to_bytes(1, "little", signed=True) + bytes(1)))
        self.queue.append((RID_KEYBOARD, bytes(8)))
        self.kick()

    def consumer(self, usage):
        self.queue.append((RID_CONSUMER, usage.to_bytes(2, "little")))
        self.queue.append((RID_CONSUMER, bytes(2)))
        self.kick()

    def reset(self):
        """Drop everything not sent yet (switched off or host gone)."""
        self.queue = []
        self.buttons = 0
        self.dx = self.dy = self.wheel = self.pan = 0.0


CSS = """
.touchpad {
  background: alpha(currentColor, 0.06);
  border-radius: 18px;
  transition: background 120ms;
}
.touchpad.touched { background: alpha(currentColor, 0.10); }
.touchpad.holding { background: alpha(@accent_bg_color, 0.25); }
.pad-button {
  background: alpha(currentColor, 0.08);
  border-radius: 12px;
  min-height: 56px;
}
.pad-button.pressed { background: alpha(@accent_bg_color, 0.35); }
.key {
  background: alpha(currentColor, 0.10);
  border-radius: 6px;
  min-height: 42px;
  min-width: 0;
  padding: 0;
  font-size: 0.85em;
}
.key.fkey { min-height: 32px; font-size: 0.75em; }
.key.pressed, .key:checked { background: alpha(@accent_bg_color, 0.45); }
.key.nav { min-height: 36px; }
.pkey {
  background: alpha(currentColor, 0.10);
  border-radius: 8px;
  min-height: 50px;
  min-width: 0;
  padding: 0;
  font-size: 1.25em;
}
.pkey.sys { background: alpha(currentColor, 0.18); font-size: 1.05em; }
.pkey.space { font-size: 0.8em; }
.pkey.return { background: @accent_bg_color; color: @accent_fg_color; }
.pkey.latched { background: alpha(@accent_bg_color, 0.45); }
.pkey.locked { background: @accent_bg_color; color: @accent_fg_color; }
.pkey.pressed { background: alpha(@accent_bg_color, 0.45); }
.pkey-popover button {
  min-width: 34px;
  min-height: 50px;
  padding: 0 2px;
  font-size: 1.25em;
  font-weight: normal;
}
"""


class LandscapeKeyboard(Gtk.Widget):
    """The keyboard page: lays the keyboard out in landscape whatever way the window is turned.

    In a portrait window the content is turned by 90 degrees (GTK turns the touches with it),
    so the keyboard always spans the long side of the screen; in a landscape window it is not
    turned. The media row goes above the keyboard only when both fit; the keyboard fills the
    rest of the page.
    """

    MARGIN = 6
    SPACING = 6

    def __init__(self, media, keyboard):
        super().__init__(hexpand=True, vexpand=True)
        self.media = media
        self.keyboard = keyboard
        media.set_parent(self)
        keyboard.set_parent(self)

    def do_dispose(self):
        for child in (self.media, self.keyboard):
            child.unparent()

    def do_measure(self, orientation, for_size):
        # Whatever the window allows; the keys shrink to fit
        return 0, 0, -1, -1

    def do_size_allocate(self, width, height, baseline):
        turned = height > width
        # The landscape area the content is laid out in
        w, h = (height, width) if turned else (width, height)
        w, h = w - 2 * self.MARGIN, h - 2 * self.MARGIN
        kb_height = max(self.keyboard.measure(Gtk.Orientation.VERTICAL, w)[1], 0)
        media_height = self.media.measure(Gtk.Orientation.VERTICAL, w)[1]
        show_media = h >= kb_height + self.SPACING + media_height
        self.media.set_child_visible(show_media)
        # The keyboard takes all the room that is left; its rows grow evenly
        kb_height = h - (media_height + self.SPACING if show_media else 0)

        def place(child, y, child_height):
            # Position in the landscape area, then into the widget: a quarter turn clockwise
            # for a portrait window (the keyboard's top along the right edge)
            t = Gsk.Transform.new()
            if turned:
                t = t.translate(Graphene.Point().init(width - self.MARGIN, self.MARGIN))
                t = t.rotate(90)
            else:
                t = t.translate(Graphene.Point().init(self.MARGIN, self.MARGIN))
            t = t.translate(Graphene.Point().init(0, y))
            child.allocate(w, child_height, -1, t)

        # The keyboard sits at the bottom of the landscape area, the media row right above it
        place(self.keyboard, h - kb_height, kb_height)
        if show_media:
            place(self.media, h - kb_height - self.SPACING - media_height, media_height)

    def do_snapshot(self, snapshot):
        for child in (self.media, self.keyboard):
            if child.get_child_visible():
                self.snapshot_child(child, snapshot)


class InputHistory(Gtk.Box):
    """What was typed on the portrait keyboard, to send again with one tap.

    An entry runs up to Enter (included); a shortcut with Ctrl or Alt is an entry of its own;
    Esc, arrows and the other navigation keys end an entry without being kept. The entry being
    typed is shown last, dimmed. Each entry keeps the strokes that were sent, so a tap sends
    exactly the same keys again. Tapping an entry moves it to the end, typing it again
    replaces the older copy.

    What is typed can include passwords: the history lives in memory only, it is gone when
    the app quits, and the button in its header clears it.
    """

    MAX_ENTRIES = 50

    def __init__(self, replay, changed):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, vexpand=True)
        self.replay = replay         # replay(strokes): send the strokes to the host
        self.changed = changed       # changed(): entries came or went
        self.entries = []            # (row, strokes), oldest first
        self.current = []            # (label, strokes) of the entry being typed
        header = Gtk.Box(spacing=6)
        header.append(Gtk.Label(label=_("History"), xalign=0, hexpand=True,
                                css_classes=["heading", "dim-label"]))
        clear = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text=_("Clear history"),
                           css_classes=["flat"])
        clear.connect("clicked", lambda _b: self.clear())
        header.append(clear)
        self.append(header)
        # Bottom-aligned, like a chat: the newest entry right above the keyboard
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.END,
                                css_classes=["boxed-list"])
        self.list.connect("row-activated", self.on_row_activated)
        self.scrolled = Gtk.ScrolledWindow(child=self.list, vexpand=True,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.append(self.scrolled)
        self.current_row = Gtk.ListBoxRow(activatable=False, visible=False)
        self.current_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                       margin_start=12, margin_end=12, margin_top=10,
                                       margin_bottom=10, css_classes=["dim-label"])
        self.current_row.set_child(self.current_label)
        self.list.append(self.current_row)

    def empty(self):
        return not self.entries and not self.current

    # ----- Recording -----

    def add(self, label, strokes):
        """One more character or key of the entry being typed."""
        self.current.append((label, strokes))
        self.show_current()

    def backspace(self):
        """Backspace takes the last character back, as it does on the host."""
        if self.current:
            self.current.pop()
            self.show_current()

    def close(self):
        """The entry being typed is finished: it becomes a history entry."""
        if self.current:
            label = "".join(lab for lab, _strokes in self.current)
            strokes = [st for _lab, part in self.current for st in part]
            self.current = []
            self.show_current()
            self.add_entry(label, strokes)

    def shortcut(self, label, strokes):
        self.close()
        self.add_entry(label, strokes)

    def show_current(self):
        text = "".join(lab for lab, _strokes in self.current)
        self.current_label.set_label(text + "…")
        self.current_row.set_visible(bool(self.current))
        self.scroll_down()
        self.changed()

    def add_entry(self, label, strokes):
        for row, old in list(self.entries):
            if old == strokes:
                self.remove_entry(row)
        row = Gtk.ListBoxRow()
        box = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=10, margin_bottom=10)
        # Spaces at the end of an entry would be invisible
        shown = label.replace(" ", "␣") if not label.strip() else label
        box.append(Gtk.Label(label=shown, xalign=0, hexpand=True,
                             ellipsize=Pango.EllipsizeMode.END))
        box.append(Gtk.Image(icon_name="media-playlist-repeat-symbolic",
                             css_classes=["dim-label"]))
        row.set_child(box)
        row.set_tooltip_text(label)
        # Before the row of the entry being typed, which stays last
        self.list.insert(row, len(self.entries))
        self.entries.append((row, strokes))
        while len(self.entries) > self.MAX_ENTRIES:
            self.remove_entry(self.entries[0][0])
        self.scroll_down()
        self.changed()

    def remove_entry(self, row):
        self.entries = [(r, st) for r, st in self.entries if r is not row]
        self.list.remove(row)

    def clear(self):
        for row, _strokes in list(self.entries):
            self.remove_entry(row)
        self.current = []
        self.show_current()

    def scroll_down(self):
        def down():
            adj = self.scrolled.get_vadjustment()
            adj.set_value(adj.get_upper())
            return False
        GLib.idle_add(down)

    # ----- Sending again -----

    def on_row_activated(self, _list, row):
        strokes = next((st for r, st in self.entries if r is row), None)
        if strokes is None:
            return
        # What was being typed goes before the repeated entry, on the host and here
        self.close()
        self.replay(strokes)
        label = row.get_tooltip_text()
        self.remove_entry(row)
        self.add_entry(label, strokes)


class PhoneKeyboard(Gtk.Box):
    """The keyboard page in a portrait window: the phone's own keyboard (Stevia) with media
    keys and a row of Esc, Tab, Ctrl, Alt and arrows above it.

    Characters are typed as a press and release of the key the host's layout has them on
    (see char_strokes); Shift latches for one letter, tapped twice it stays on. Keys with
    alternates wait for the finger to lift, a long press shows the alternates instead; touching
    another key types a waiting one first, so fast typing keeps its order. Backspace, space,
    Enter and the navigation keys stay down while touched, so the host repeats them.
    """

    SHIFT_OFF, SHIFT_ONCE, SHIFT_LOCKED = range(3)
    DOUBLE_TAP_MS = 400
    UNITS = 10      # key units per row, as on the phone: ten letters in the top row
    COLUMNS = 2     # grid columns per key unit, for keys of 1.5 units

    def __init__(self, get_reports, history):
        # vexpand set explicitly: the keys expand to fill their rows, which must not make the
        # whole keyboard take room from the history above it
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.END,
                         vexpand=False)
        self.get_reports = get_reports
        self.history = history       # InputHistory: what is typed here is recorded there
        self.layout = None
        self.strokes = {}
        self.shift = self.SHIFT_OFF
        self.shift_time = 0
        self.shift_key = None
        self.letter_labels = []      # (label, lowercase character) of the letters layer
        self.held = []               # usages of keys held down right now
        self.pending = None          # (character, letter, key) waiting for the finger to lift
        self.long_press = 0
        self.popover = None
        self.modifiers = {}          # Ctrl and Alt of the navigation row: bit -> toggle
        self.append(self.build_media())
        self.append(self.build_nav())
        self.layers = Gtk.Stack()
        self.append(self.layers)

    # ----- Rows above the keys -----

    def build_media(self):
        row = Gtk.Box(spacing=4, homogeneous=True)
        buttons = [("audio-volume-low-symbolic", CC_VOL_DOWN, _("Volume down")),
                   ("audio-volume-muted-symbolic", CC_MUTE, _("Mute")),
                   ("audio-volume-high-symbolic", CC_VOL_UP, _("Volume up")),
                   ("media-skip-backward-symbolic", CC_PREVIOUS, _("Previous track")),
                   ("media-playback-start-symbolic", CC_PLAY_PAUSE, _("Play or pause")),
                   ("media-skip-forward-symbolic", CC_NEXT, _("Next track")),
                   ("go-previous-symbolic", CC_AC_BACK, _("Back")),
                   ("go-home-symbolic", CC_AC_HOME, _("Home"))]
        for icon, usage, tip in buttons:
            btn = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["key"])
            btn.connect("clicked", lambda _b, u=usage: self.consumer(u))
            row.append(btn)
        return row

    def build_nav(self):
        """Esc, Tab, Ctrl and Alt, arrows: what the phone's keyboard shows in a terminal."""
        row = Gtk.Box(spacing=4, homogeneous=True)
        self.nav_keys = {}
        for usage in (KEY_ESC, KEY_TAB):
            row.append(self.held_key(usage, ["key", "nav"]))
        for bit in (MOD_CTRL, MOD_ALT):
            toggle = Gtk.ToggleButton(css_classes=["key", "nav"], hexpand=True)
            self.modifiers[bit] = toggle
            row.append(toggle)
        for usage in (KEY_LEFT, KEY_UP, KEY_DOWN, KEY_RIGHT):
            row.append(self.held_key(usage, ["key", "nav"]))
        return row

    def consumer(self, usage):
        reports = self.get_reports()
        if reports:
            reports.consumer(usage)

    # ----- Layers -----

    def set_layout(self, layout):
        """(Re)build the layers for the host's layout: labels and what each key sends."""
        self.release()
        self.layout = layout
        self.strokes = char_strokes(layout)
        de = layout == "de"
        self.modifiers[MOD_CTRL].set_label("Strg" if de else "Ctrl")
        self.modifiers[MOD_ALT].set_label("Alt")
        for usage, key in self.nav_keys.items():
            key.get_first_child().set_label(self.label(usage))
        for name in ("letters", "symbols", "symbols2"):
            old = self.layers.get_child_by_name(name)
            if old:
                self.layers.remove(old)
        self.letter_labels = []
        self.shift_key = None
        for name, (rows, extras) in phone_layers(layout).items():
            self.layers.add_named(self.build_layer(name, rows, extras), name)
        self.show_layer("letters")

    def show_layer(self, name):
        self.layers.set_visible_child_name(name)
        self.set_shift(self.SHIFT_OFF)

    def build_layer(self, name, rows, extras):
        grid = Gtk.Grid(row_spacing=6, column_spacing=4, column_homogeneous=True,
                        row_homogeneous=True)
        letters = name == "letters"
        span = self.COLUMNS

        def attach(widget, col, row, width):
            grid.attach(widget, round(col * span), row, round(width * span), 1)

        for r, chars in enumerate(rows):
            if chars is None:
                # Second symbol layer: navigation keys instead of characters
                keys = [self.held_key(u, ["pkey", "sys"]) for u in
                        (KEY_HOME, KEY_END, KEY_PAGEUP, KEY_PAGEDOWN, KEY_INSERT, KEY_DELETE)]
                width = 1.5
            else:
                chars = [c for c in chars if c in self.strokes]
                keys = [self.char_key(c, letters) for c in chars]
                # The third row shares what is left between Shift and Backspace
                width = 7 / len(keys) if r == 2 else 1
            col = (self.UNITS - width * len(keys)) / 2
            for key in keys:
                attach(key, col, r, width)
                col += width
        # Third row: Shift on letters, the other symbol layer on symbols; Backspace
        if letters:
            self.shift_key = self.make_key("⇧", ["pkey", "sys"], self.on_shift)
            attach(self.shift_key, 0, 2, 1.5)
        else:
            other, label = ("symbols", "123") if name == "symbols2" else ("symbols2", "={<")
            attach(self.make_key(label, ["pkey", "sys"], lambda *_: self.show_layer(other)),
                   0, 2, 1.5)
        attach(self.held_key(KEY_BACKSPACE, ["pkey", "sys"]), self.UNITS - 1.5, 2, 1.5)
        # Bottom row: layer switch, extras, comma, space, full stop, Enter
        target, label = ("symbols", "123") if letters else ("letters", "ABC")
        attach(self.make_key(label, ["pkey", "sys"], lambda *_: self.show_layer(target)),
               0, 3, 1.5)
        col = 1.5
        for c in list(extras) + [","]:
            attach(self.char_key(c, False), col, 3, 1)
            col += 1
        space_width = self.UNITS - col - 1 - 1.5
        space = self.held_key(KEY_SPACE, ["pkey", "space"])
        space.get_first_child().set_label(_("German") if self.layout == "de"
                                          else _("English (US)"))
        space.get_first_child().add_css_class("dim-label")
        attach(space, col, 3, space_width)
        attach(self.char_key(".", False), col + space_width, 3, 1)
        attach(self.held_key(KEY_ENTER, ["pkey", "return"]), self.UNITS - 1.5, 3, 1.5)
        return grid

    def label(self, usage):
        de = self.layout == "de"
        return {KEY_ESC: "Esc", KEY_TAB: "⇥", KEY_ENTER: "⏎", KEY_BACKSPACE: "⌫",
                KEY_SPACE: "", KEY_DELETE: "Entf" if de else "Del",
                KEY_INSERT: "Einfg" if de else "Ins", KEY_HOME: "Pos1" if de else "Home",
                KEY_END: "Ende" if de else "End", KEY_PAGEUP: "Bild↑" if de else "PgUp",
                KEY_PAGEDOWN: "Bild↓" if de else "PgDn", KEY_LEFT: "←", KEY_RIGHT: "→",
                KEY_UP: "↑", KEY_DOWN: "↓"}.get(usage, "?")

    # ----- Keys -----

    def make_key(self, label, css, down, up=None):
        """A key that reacts when touched, not when released, like the phone's keyboard."""
        box = Gtk.Box(hexpand=True, vexpand=True, css_classes=css)
        box.append(Gtk.Label(label=label, hexpand=True, can_target=False))
        gesture = Gtk.GestureDrag()

        def on_down(*_):
            box.add_css_class("pressed")
            down(box)

        def on_up(*_):
            box.remove_css_class("pressed")
            if up:
                up(box)
        gesture.connect("drag-begin", on_down)
        gesture.connect("drag-end", on_up)
        gesture.connect("cancel", on_up)
        box.add_controller(gesture)
        return box

    def char_key(self, char, letter):
        alternates = [a for a in ALTERNATES.get(char, "") if a in self.strokes]

        def down(box):
            self.flush()
            if alternates:
                self.pending = (char, letter, box)
                self.long_press = GLib.timeout_add(LONG_PRESS_MS, self.on_long_press,
                                                   [char] + alternates, letter, box)
            else:
                self.type(self.shifted(char, letter))

        def up(box):
            if self.pending and self.pending[2] is box:
                self.flush()
        key = self.make_key(char, ["pkey"], down, up)
        if letter:
            self.letter_labels.append((key.get_first_child(), char))
        return key

    def held_key(self, usage, css):
        """Down while touched, so the host repeats it; Ctrl and Alt go with it."""
        def down(_box):
            self.flush()
            reports = self.get_reports()
            if reports and usage not in self.held:
                self.held.append(usage)
                reports.keys(self.latched(), self.held)
                self.record_key(usage, self.latched())

        def up(_box):
            reports = self.get_reports()
            if reports and usage in self.held:
                self.held.remove(usage)
                if self.held:
                    reports.keys(self.latched(), self.held)
                else:
                    self.take_modifiers()
                    reports.keys(0, [])
        key = self.make_key(self.label(usage), css, down, up)
        if "nav" in css:
            self.nav_keys[usage] = key
        return key

    def shifted(self, char, letter):
        upper = char.upper()
        if letter and self.shift != self.SHIFT_OFF and len(upper) == 1 and upper in self.strokes:
            return upper
        return char

    def type(self, char):
        """Type one character: its strokes, Ctrl or Alt with the last one."""
        reports = self.get_reports()
        strokes = self.strokes.get(char)
        if not reports or not strokes:
            return
        latched = self.take_modifiers()
        sent = [(usage, mods | (latched if i == len(strokes) - 1 else 0))
                for i, (usage, mods) in enumerate(strokes)]
        for usage, mods in sent:
            reports.key(mods, usage)
        if latched:
            self.history.shortcut(self.combo_label(latched, char), sent)
        else:
            self.history.add(char, sent)
        if self.shift == self.SHIFT_ONCE:
            self.set_shift(self.SHIFT_OFF)

    def record_key(self, usage, mods):
        """Note a held key in the history: space, Tab and Enter belong to the text, Backspace
        takes a character back, a shortcut is an entry of its own, the other keys end the
        entry being typed."""
        if mods:
            self.history.shortcut(self.combo_label(mods, self.label(usage) or "␣"),
                                  [(usage, mods)])
        elif usage == KEY_SPACE:
            self.history.add(" ", [(KEY_SPACE, 0)])
        elif usage == KEY_TAB:
            self.history.add("⇥", [(KEY_TAB, 0)])
        elif usage == KEY_ENTER:
            self.history.add("⏎", [(KEY_ENTER, 0)])
            self.history.close()
        elif usage == KEY_BACKSPACE:
            self.history.backspace()
        else:
            self.history.close()

    def combo_label(self, mods, text):
        """"Strg+Alt+C" and the like, for shortcuts in the history."""
        names = []
        if mods & MOD_CTRL:
            names.append("Strg" if self.layout == "de" else "Ctrl")
        if mods & MOD_ALT:
            names.append("Alt")
        return "+".join(names + [text.upper() if len(text) == 1 else text])

    def replay(self, strokes):
        """Send recorded strokes again, see InputHistory."""
        reports = self.get_reports()
        if reports:
            for usage, mods in strokes:
                reports.key(mods, usage)

    def flush(self):
        """Type the key that waits for its finger to lift."""
        if self.long_press:
            GLib.source_remove(self.long_press)
            self.long_press = 0
        if self.pending:
            char, letter, _box = self.pending
            self.pending = None
            self.type(self.shifted(char, letter))

    def on_long_press(self, chars, letter, box):
        """Held long enough: offer the alternates; the key itself types nothing then."""
        self.long_press = 0
        self.pending = None
        row = Gtk.Box(spacing=4)
        popover = Gtk.Popover(child=row, position=Gtk.PositionType.TOP, has_arrow=False,
                              css_classes=["pkey-popover"])
        for c in chars:
            c = self.shifted(c, letter)
            btn = Gtk.Button(label=c)
            btn.connect("clicked", lambda _b, ch=c: (popover.popdown(), self.type(ch)))
            row.append(btn)
        popover.set_parent(box)
        self.place_popover(popover, box)
        popover.connect("closed", lambda p: GLib.idle_add(self.drop_popover, p))
        self.popover = popover
        popover.popup()
        return False

    def place_popover(self, popover, box):
        """Above the key, moved sideways to stay inside the window: keys at the edge would
        otherwise leave part of it off screen where the compositor does not slide popups."""
        root = box.get_root()
        ok, point = box.compute_point(root, Graphene.Point().init(0, 0))
        if not ok:
            return
        width = popover.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
        root_width = root.get_width()
        center = point.x + box.get_width() / 2
        center = max(width / 2, min(root_width - width / 2, center))
        rect = Gdk.Rectangle()
        rect.x, rect.y = int(center - point.x) - 1, 0
        rect.width, rect.height = 2, box.get_height()
        popover.set_pointing_to(rect)

    def drop_popover(self, popover):
        if self.popover is popover:
            self.popover = None
        popover.unparent()
        return False

    def on_shift(self, _box):
        """One tap: the next letter is a capital; two quick taps: capitals until tapped again."""
        self.flush()
        now = GLib.get_monotonic_time()
        quick = (now - self.shift_time) / 1000.0 < self.DOUBLE_TAP_MS
        self.shift_time = now
        if self.shift == self.SHIFT_ONCE and quick:
            self.set_shift(self.SHIFT_LOCKED)
        elif self.shift == self.SHIFT_OFF:
            self.set_shift(self.SHIFT_ONCE)
        else:
            self.set_shift(self.SHIFT_OFF)

    def set_shift(self, state):
        self.shift = state
        if self.shift_key:
            self.shift_key.get_first_child().set_label(
                {self.SHIFT_OFF: "⇧", self.SHIFT_ONCE: "⬆", self.SHIFT_LOCKED: "⇪"}[state])
            for css, on in (("latched", state == self.SHIFT_ONCE),
                            ("locked", state == self.SHIFT_LOCKED)):
                if on:
                    self.shift_key.add_css_class(css)
                else:
                    self.shift_key.remove_css_class(css)
        for label, char in self.letter_labels:
            label.set_label(self.shifted(char, True))

    def latched(self):
        return sum(bit for bit, toggle in self.modifiers.items() if toggle.get_active())

    def take_modifiers(self):
        """Ctrl and Alt that are on; they let go after this key."""
        mods = self.latched()
        for toggle in self.modifiers.values():
            toggle.set_active(False)
        return mods

    def release(self):
        """Let go of everything: page left, layout changed, host gone."""
        if self.long_press:
            GLib.source_remove(self.long_press)
            self.long_press = 0
        self.pending = None
        if self.popover:
            self.popover.popdown()
        if self.held:
            reports = self.get_reports()
            if reports:
                reports.keys(0, [])
            self.held = []


class RemoteApp(Adw.Application):
    def __init__(self, desktop=False):
        # Command line handled here: "--touchpad"/"--keyboard" open that page (GNOME Settings'
        # Mouse & Touchpad and Keyboard pages start us like this, see the desktop file's actions).
        # --desktop is an instance of its own: the Bluetooth one, if open, stays as it is.
        super().__init__(application_id=DESKTOP_APP_ID if desktop else APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.desktop_mode = desktop
        self.peripheral = None
        self.reports = None
        self.dialog = None
        self.dialog_key = None
        self.speed = 1.6
        self.natural = True
        self.known_hosts = set()
        self.shown_state = None
        # Touch state of the pad
        self.drag_last = (0.0, 0.0)
        self.drag_time = 0
        self.touch_start = 0
        self.moved = 0.0
        self.two_fingers = False      # a second finger came down during this touch
        self.zoom_center = None
        self.zoom_start = 0
        self.zoom_moved = 0.0
        self.long_press = 0
        self.holding = False          # left button held by a long press
        # Full keyboard: key labels after the host's layout, keys held right now
        self.layout = default_layout()
        self.held_keys = []
        self.modifiers = {}
        # Touchpad: touches on the pad (for three fingers), tap and drag
        self.touches = {}
        self.max_touches = 0
        self.three_start = None
        self.three_switched = 0
        self.three_swiped = False
        self.last_tap = 0
        self.tap_drag = False
        self.zoom_mode = None
        self.zoom_scale = 1.0
        # --desktop: input for this phone's desktop on a TV, not for a Bluetooth host
        self.local = None
        self.schwupp_watch = 0

    # ----- Settings -----

    def load_settings(self):
        kf = GLib.KeyFile()
        try:
            kf.load_from_file(SETTINGS_FILE, GLib.KeyFileFlags.NONE)
        except GLib.Error:
            return
        # Each key on its own, so one missing key does not lose the others
        for read in (lambda: setattr(self, "speed", kf.get_double("touchpad", "speed")),
                     lambda: setattr(self, "natural",
                                     kf.get_boolean("touchpad", "natural-scrolling")),
                     lambda: self.known_hosts.update(kf.get_string_list("hosts", "known")),
                     lambda: setattr(self, "layout", kf.get_string("keyboard", "layout"))):
            try:
                read()
            except GLib.Error:
                pass

    def save_settings(self):
        kf = GLib.KeyFile()
        kf.set_double("touchpad", "speed", self.speed)
        kf.set_boolean("touchpad", "natural-scrolling", self.natural)
        kf.set_string("keyboard", "layout", self.layout)
        # BlueZ object paths of hosts that used us, to recognise them when they reconnect
        kf.set_string_list("hosts", "known", sorted(self.known_hosts))
        try:
            os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
            kf.save_to_file(SETTINGS_FILE)
        except (GLib.Error, OSError) as e:
            log(f"saving settings failed: {e}")

    # ----- UI -----

    def do_command_line(self, command_line):
        args = command_line.get_arguments()[1:]
        self.activate()
        for page in ("touchpad", "keyboard"):
            if "--" + page in args:
                self.stack.set_visible_child_name(page)
        if "--desktop" in args:
            self.start_local()
        return 0

    # ----- The desktop on a TV -----

    def as_layer(self, win):
        """The whole phone screen, above the shell, and no keyboard focus ever (needs
        gtk4-layer-shell; without it the app is a full-screen window)."""
        try:
            gi.require_version("Gtk4LayerShell", "1.0")
            from gi.repository import Gtk4LayerShell as Layer
        except (ImportError, ValueError):
            log("no gtk4-layer-shell: a window instead of a layer")
            return False
        if not Layer.is_supported():
            return False
        Layer.init_for_window(win)
        Layer.set_namespace(win, "plinius-remote")
        Layer.set_layer(win, Layer.Layer.OVERLAY)
        Layer.set_keyboard_mode(win, Layer.KeyboardMode.NONE)
        for edge in (Layer.Edge.TOP, Layer.Edge.BOTTOM, Layer.Edge.LEFT, Layer.Edge.RIGHT):
            Layer.set_anchor(win, edge, True)
        phone = phone_monitor()
        if phone is not None:
            Layer.set_monitor(win, phone)
        self.layered = True
        return True

    def start_local(self):
        """Touchpad and keyboard for this phone's desktop on a TV, in full screen."""
        if self.local is None:
            try:
                self.local = LocalInput()
            except (ImportError, OSError) as exc:
                log(f"no virtual mouse and keyboard: {exc}")
                self.toast(_("The desktop cannot get a mouse and keyboard from here."))
                return
            if self.peripheral:
                self.peripheral.stop()
            self.reports.reset()
            self.schwupp_watch = Gio.bus_watch_name(
                Gio.BusType.SESSION, SCHWUPP_BUS, Gio.BusNameWatcherFlags.NONE,
                self.on_schwupp_appeared, self.on_schwupp_vanished)
            # phoc opens a window on the screen with the pointer: the pointer goes to the TV
            # only once this window stands on the phone
            win = self.props.active_window
            if win.get_mapped():
                GLib.timeout_add(POINTER_DELAY_MS, self.pointer_to_tv)
            else:
                win.connect("map", lambda _w: GLib.timeout_add(POINTER_DELAY_MS,
                                                               self.pointer_to_tv))
        self.show_local()
        if not getattr(self, "layered", False):
            self.props.active_window.fullscreen()

    def pointer_to_tv(self):
        if self.local is not None:
            self.local.pointer_to_tv()
        return False

    def show_local(self):
        self.switch.set_sensitive(False)
        self.title.set_subtitle(_("Desktop on the TV"))
        self.pad_label.set_label(_("Move the pointer on the TV with one finger, tap to click. "
                                   "Two fingers: tap for the right button, move to scroll."))

    def on_schwupp_appeared(self, conn, _name, _owner):
        conn.signal_subscribe(SCHWUPP_BUS, PROPS_IFACE, "PropertiesChanged", SCHWUPP_PATH,
                              SCHWUPP_IFACE, Gio.DBusSignalFlags.NONE, self.on_schwupp_changed)

    def on_schwupp_changed(self, _conn, _sender, _path, _iface, _signal, params):
        _iface_name, changed, _invalid = params.unpack()
        if "Source" in changed and changed["Source"] != "desktop":
            self.end_local()

    def on_schwupp_vanished(self, _conn, _name):
        if self.local is not None:
            self.end_local()

    def end_local(self):
        """The TV no longer shows the desktop: the app is done."""
        log("the desktop on the TV ended")
        self.quit()

    def do_activate(self):
        if self.props.active_window:
            self.props.active_window.present()
            return
        self.load_settings()
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        win = Adw.ApplicationWindow(application=self, title=_("Remote Input"),
                                    default_width=360, default_height=720)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title=_("Remote Input"))
        header.set_title_widget(self.title)
        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER,
                                 tooltip_text=_("Show up as mouse and keyboard"))
        self.switch_handler = self.switch.connect("notify::active", self.on_switch)
        header.pack_start(self.switch)
        header.pack_end(self.build_menu())
        toolbar.add_top_bar(header)

        self.stack = Adw.ViewStack()
        self.stack.add_titled_with_icon(self.build_touchpad(), "touchpad", _("Touchpad"),
                                        "input-touchpad-symbolic")
        self.stack.add_titled_with_icon(self.build_keyboard(), "keyboard", _("Keyboard"),
                                        "input-keyboard-symbolic")
        self.toast_overlay = Adw.ToastOverlay(child=self.stack)
        toolbar.set_content(self.toast_overlay)
        switcher_bar = Adw.ViewSwitcherBar(stack=self.stack, reveal=True)
        toolbar.add_bottom_bar(switcher_bar)
        # In landscape the page switcher moves from the bottom into the header bar (top right),
        # so the keyboard gets the height of the bottom bar
        header_switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE,
                                           visible=False)
        header.pack_end(header_switcher)
        landscape = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("min-aspect-ratio: 1/1"))
        landscape.add_setter(switcher_bar, "reveal", False)
        landscape.add_setter(header_switcher, "visible", True)
        # The keyboard page: the phone's keyboard in portrait, the full keyboard in landscape
        landscape.add_setter(self.kb_stack, "visible-child-name", "landscape")
        win.add_breakpoint(landscape)
        win.set_content(toolbar)

        self.reports = Reports(self.send_report)
        self.peripheral = Peripheral(self.on_peripheral_changed, self.toast, self.on_pair,
                                     self.on_pair_cancel, self.known_hosts,
                                     lambda _path: self.save_settings())
        GLib.timeout_add_seconds(60, self.on_battery_timer)
        self.on_peripheral_changed()
        if self.desktop_mode:
            self.as_layer(win)
        win.present()
        # Nothing that could open the phone's on-screen keyboard gets the focus by itself
        win.set_focus(None)

    def build_menu(self):
        """Pointer speed and scroll direction, in a popover from the header bar."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6,
                      margin_bottom=6, margin_start=6, margin_end=6)
        box.append(Gtk.Label(label=_("Pointer speed"), xalign=0))
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.5, 4.0, 0.1)
        scale.set_value(self.speed)
        scale.set_size_request(220, -1)
        scale.connect("value-changed", self.on_speed)
        box.append(scale)
        natural = Gtk.CheckButton(label=_("Natural scrolling"), active=self.natural)
        natural.connect("toggled", self.on_natural)
        box.append(natural)
        # The host's keyboard layout decides what a key types; the labels follow it
        box.append(Gtk.Label(label=_("Key labels"), xalign=0, margin_top=6))
        us = Gtk.CheckButton(label=_("English (US)"), active=self.layout != "de")
        de = Gtk.CheckButton(label=_("German"), active=self.layout == "de", group=us)
        us.connect("toggled", lambda b: b.get_active() and self.set_layout("us"))
        de.connect("toggled", lambda b: b.get_active() and self.set_layout("de"))
        box.append(us)
        box.append(de)
        return Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text=_("Settings"),
                              popover=Gtk.Popover(child=box))

    def on_speed(self, scale):
        self.speed = round(scale.get_value(), 1)
        self.save_settings()

    def on_natural(self, check):
        self.natural = check.get_active()
        self.save_settings()

    def build_touchpad(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8,
                       margin_bottom=8, margin_start=12, margin_end=12)
        self.pad = Gtk.Box(vexpand=True, hexpand=True, css_classes=["touchpad"])
        self.pad_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, hexpand=True,
                                   valign=Gtk.Align.CENTER, margin_start=24, margin_end=24,
                                   css_classes=["dim-label"], can_target=False)
        self.pad.append(self.pad_label)

        # One finger: GestureDrag. Two fingers: GestureZoom, which tracks the centre of both.
        # Neither grouped nor claimed: GestureDrag handles one touch only and ended at the
        # second one, taking a grouped GestureZoom with it, and a touch it claimed was denied
        # to GestureZoom - two fingers made a right click instead of scrolling.
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self.on_drag_begin)
        drag.connect("drag-update", self.on_drag_update)
        drag.connect("drag-end", self.on_drag_end)
        drag.connect("cancel", lambda *_: self.end_touch())
        zoom = Gtk.GestureZoom()
        zoom.connect("begin", self.on_two_begin)
        zoom.connect("update", self.on_two_update)
        zoom.connect("end", self.on_two_end)
        zoom.connect("cancel", lambda *_: setattr(self, "zoom_center", None))
        # All touches, for three-finger taps and swipes; added first so the count is up to
        # date when the gestures above see the same events
        legacy = Gtk.EventControllerLegacy()
        legacy.connect("event", self.on_pad_event)
        self.pad.add_controller(legacy)
        self.pad.add_controller(drag)
        self.pad.add_controller(zoom)
        page.append(self.pad)

        bar = Gtk.Box(spacing=8, homogeneous=False)
        bar.append(self.pad_button(_("Left"), 1))
        self.hold_toggle = Gtk.ToggleButton(icon_name="object-select-symbolic",
                                            tooltip_text=_("Hold the left button"),
                                            css_classes=["pad-button"], width_request=64)
        self.hold_toggle.connect("toggled", self.on_hold_toggled)
        bar.append(self.hold_toggle)
        bar.append(self.pad_button(_("Right"), 2))
        page.append(bar)
        return page

    def pad_button(self, label, button):
        """A mouse button: pressed while the finger is on it, so it can drag together with
        the pad. A plain Gtk.Button would only report the click after release."""
        box = Gtk.Box(hexpand=True, css_classes=["pad-button"])
        box.append(Gtk.Label(label=label, hexpand=True, can_target=False))
        gesture = Gtk.GestureDrag()

        def down(*_):
            box.add_css_class("pressed")
            if self.reports:
                self.reports.set_buttons(self.reports.buttons | button)

        def up(*_):
            box.remove_css_class("pressed")
            if self.reports:
                self.reports.set_buttons(self.reports.buttons & ~button)
        gesture.connect("drag-begin", down)
        gesture.connect("drag-end", up)
        gesture.connect("cancel", up)
        box.add_controller(gesture)
        return box

    def build_keyboard(self):
        """Portrait: a keyboard like the phone's own, media keys above it. Landscape: a full
        keyboard without number pad, F-keys on top; media keys above it when there is room."""
        # The free room above the portrait keyboard: the history of what was typed, or as
        # long as there is none, a hint where the keys go
        self.history = InputHistory(lambda strokes: self.phone.replay(strokes),
                                    self.on_history_changed)
        self.phone = PhoneKeyboard(lambda: self.reports, self.history)
        self.phone.set_layout(self.layout)
        self.kb_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, vexpand=True,
                                  valign=Gtk.Align.CENTER, margin_start=24, margin_end=24,
                                  css_classes=["dim-label"])
        portrait = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6,
                           margin_bottom=6, margin_start=6, margin_end=6)
        portrait.append(self.kb_label)
        portrait.append(self.history)
        portrait.append(self.phone)
        self.on_history_changed()
        # Not homogeneous: the landscape keyboard must not get the portrait page's height,
        # it decides from its own height whether the media row fits
        self.kb_stack = Gtk.Stack(hhomogeneous=False, vhomogeneous=False)
        self.kb_stack.add_named(portrait, "portrait")
        self.kb_stack.add_named(self.build_landscape_keyboard(), "landscape")
        # Turning the phone with a finger on a key: nothing stays held on the host
        self.kb_stack.connect("notify::visible-child-name",
                              lambda *_: (self.release_keys(), self.phone.release()))
        return self.kb_stack

    def on_history_changed(self):
        empty = self.history.empty()
        self.history.set_visible(not empty)
        self.kb_label.set_visible(empty)

    def build_landscape_keyboard(self):
        """A full keyboard without number pad, F-keys on top; media keys above it when there
        is room."""
        self.keyboard_bin = Adw.Bin()
        self.fill_keyboard()
        media = Gtk.Box(spacing=4, homogeneous=True)
        buttons = [("audio-volume-low-symbolic", CC_VOL_DOWN, _("Volume down")),
                   ("audio-volume-muted-symbolic", CC_MUTE, _("Mute")),
                   ("audio-volume-high-symbolic", CC_VOL_UP, _("Volume up")),
                   ("media-playback-start-symbolic", CC_PLAY_PAUSE, _("Play or pause")),
                   ("go-previous-symbolic", CC_AC_BACK, _("Back")),
                   ("go-home-symbolic", CC_AC_HOME, _("Home"))]
        for icon, usage, tip in buttons:
            btn = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["key"])
            btn.connect("clicked", lambda _b, u=usage: self.reports and self.reports.consumer(u))
            media.append(btn)
        return LandscapeKeyboard(media, self.keyboard_bin)

    def set_layout(self, layout):
        if layout != self.layout:
            self.layout = layout
            self.save_settings()
            self.fill_keyboard()
            self.phone.set_layout(layout)

    def fill_keyboard(self):
        self.release_keys()
        self.modifiers = {}
        labels = LABELS_DE if self.layout == "de" else LABELS_US
        # Four grid columns per key unit, for keys of 1.25, 1.5, 1.75 ... units
        grid = Gtk.Grid(row_spacing=4, column_spacing=3, column_homogeneous=True,
                        row_homogeneous=True)
        for row, keys in enumerate(keyboard_rows(self.layout)):
            col = 0
            for kind, value, width in keys:
                span = round(width * 4)
                if kind == "mod":
                    widget = self.modifier_key(value)
                elif kind != "gap":
                    label = (labels.get(value, "?") if kind == "key"
                             else f"F{value - KEY_F1 + 1}" if kind == "fkey"
                             else self.special_label(value))
                    widget = self.key_button(label, value, fkey=kind == "fkey")
                if kind != "gap":
                    grid.attach(widget, col, row, span, 1)
                col += span
        self.keyboard_bin.set_child(grid)

    def special_label(self, usage):
        de = self.layout == "de"
        return {KEY_ESC: "Esc", KEY_TAB: "⇥", KEY_CAPS: "⇪", KEY_ENTER: "⏎",
                KEY_BACKSPACE: "⌫", KEY_SPACE: "", KEY_DELETE: "Entf" if de else "Del",
                KEY_HOME: "Pos1" if de else "Home", KEY_END: "Ende" if de else "End",
                KEY_PAGEUP: "Bild↑" if de else "PgUp", KEY_PAGEDOWN: "Bild↓" if de else "PgDn",
                KEY_LEFT: "←", KEY_RIGHT: "→", KEY_UP: "↑", KEY_DOWN: "↓"}.get(usage, "?")

    def modifier_key(self, bit):
        """A modifier latches for the next key, like Shift on a phone keyboard."""
        de = self.layout == "de"
        label = {MOD_CTRL: "Strg" if de else "Ctrl", MOD_RCTRL: "Strg" if de else "Ctrl",
                 MOD_SHIFT: "⇧", 0x20: "⇧", MOD_ALT: "Alt", MOD_RALT: "AltGr",
                 MOD_SUPER: "⊞"}[bit]
        toggle = Gtk.ToggleButton(label=label, css_classes=["key"], hexpand=True, vexpand=True)
        self.modifiers[bit] = toggle
        return toggle

    def key_button(self, label, usage, fkey=False):
        """A key that is down while the finger is on it, so the host repeats it when held."""
        box = Gtk.Box(hexpand=True, vexpand=True, css_classes=["key", "fkey"] if fkey else ["key"])
        box.append(Gtk.Label(label=label, hexpand=True, can_target=False))
        gesture = Gtk.GestureDrag()

        def down(*_):
            box.add_css_class("pressed")
            if self.reports and usage not in self.held_keys:
                self.held_keys.append(usage)
                self.reports.keys(self.latched_modifiers(), self.held_keys)

        def up(*_):
            box.remove_css_class("pressed")
            if self.reports and usage in self.held_keys:
                self.held_keys.remove(usage)
                if self.held_keys:
                    self.reports.keys(self.latched_modifiers(), self.held_keys)
                else:
                    # The last key is up: latched modifiers let go with it
                    self.take_modifiers()
                    self.reports.keys(0, [])
        gesture.connect("drag-begin", down)
        gesture.connect("drag-end", up)
        gesture.connect("cancel", up)
        box.add_controller(gesture)
        return box

    def latched_modifiers(self):
        return sum(bit for bit, toggle in self.modifiers.items() if toggle.get_active())

    def release_keys(self):
        if self.held_keys and self.reports:
            self.reports.keys(0, [])
        self.held_keys = []

    def toast(self, text):
        self.toast_overlay.add_toast(Adw.Toast(title=text))

    # ----- State shown in the UI -----

    def on_peripheral_changed(self):
        p = self.peripheral
        if not p:
            return
        if self.local is not None:
            self.show_local()        # input goes to the TV's desktop, Bluetooth stays off
            return
        state = p.state
        self.switch.handler_block(self.switch_handler)
        self.switch.set_active(state in (p.STARTING, p.VISIBLE, p.CONNECTED))
        self.switch.handler_unblock(self.switch_handler)
        self.switch.set_sensitive(p.available() and state != p.STOPPING)
        if not p.available():
            subtitle = _("Bluetooth is not available")
            hint = _("Bluetooth is not available on this phone right now.")
        elif state == p.OFF:
            subtitle = _("Not visible")
            hint = _("Turn on the switch to use this phone as mouse and keyboard for a "
                     "computer, tablet or TV.")
        elif state == p.STARTING:
            subtitle = _("Starting…")
            hint = ""
        elif state == p.VISIBLE:
            subtitle = _("Visible as “{name}”").format(name=p.name)
            hint = _("On the other device, open the Bluetooth settings and pair with "
                     "“{name}”.").format(name=p.name)
        elif state == p.CONNECTED:
            host = p.host_name()
            subtitle = (_("Connected to {host}").format(host=host) if host
                        else _("Connected"))
            hint = _("Move with one finger, tap to click, tap and touch again to drag. "
                     "Two fingers: tap for the right button, move to scroll, spread or "
                     "pinch to zoom. Three fingers: tap for the middle button, swipe "
                     "sideways to switch windows, up for the overview.")
        else:
            subtitle = _("Stopping…")
            hint = ""
        if state != self.shown_state:
            # Buttons held and keys queued for a host that is gone must not reach the next one
            if self.shown_state == p.CONNECTED and self.reports:
                self.phone.release()
                self.reports.reset()
                self.hold_toggle.set_active(False)
            self.shown_state = state
        self.title.set_subtitle(subtitle)
        self.pad_label.set_label(hint)
        if state == p.CONNECTED:
            host = p.host_name()
            self.kb_label.set_label(_("Typing goes to {host}.").format(host=host) if host
                                    else "")
        else:
            self.kb_label.set_label(hint)

    def on_switch(self, switch, _pspec):
        if switch.get_active():
            self.peripheral.start()
        else:
            self.peripheral.stop()

    def send_report(self, rid, data):
        if self.local is not None:
            self.local.send(rid, data)
        else:
            self.peripheral.notify(rid, data)

    def on_battery_timer(self):
        self.peripheral.update_battery()
        return True

    # ----- Pairing questions -----

    def on_pair(self, kind, name, code, answer):
        # BlueZ repeats DisplayPasskey for every digit typed on the host; keep the dialog
        if self.dialog and self.dialog_key == (kind, name, code):
            return
        self.on_pair_cancel()
        self.dialog_key = (kind, name, code)
        if kind == "display":
            heading = _("Pair with {name}").format(name=name)
            body = _("Type this code on {name}:\n\n{code}").format(name=name, code=code)
        elif kind == "confirm":
            heading = _("Pair with {name}?").format(name=name)
            body = _("Make sure {name} shows the same code:\n\n{code}").format(name=name,
                                                                                code=code)
        else:
            heading = _("Pair with {name}?").format(name=name)
            body = _("{name} can then use this phone as mouse and keyboard.").format(name=name)
        dialog = Adw.AlertDialog(heading=heading, body=body)
        if kind == "display":
            dialog.add_response("ok", _("Close"))
        else:
            dialog.add_response("reject", _("Reject"))
            dialog.add_response("pair", _("Pair"))
            dialog.set_response_appearance("pair", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_close_response("reject")
        def response(d, r):
            if self.dialog is d:
                self.dialog = None
            answer(r == "pair")
        dialog.connect("response", response)
        self.dialog = dialog
        dialog.present(self.props.active_window)

    def on_pair_cancel(self):
        if self.dialog:
            dialog, self.dialog = self.dialog, None
            dialog.force_close()

    # ----- Touchpad gestures -----

    def on_drag_begin(self, _gesture, _x, _y):
        now = GLib.get_monotonic_time()
        self.drag_last = (0.0, 0.0)
        self.drag_time = self.touch_start = now
        self.moved = 0.0
        self.two_fingers = False
        self.pad.add_css_class("touched")
        self.cancel_long_press()
        # Tap and drag: touching again right after a tap holds the left button; lifting
        # without moving makes it the second click of a double click
        self.tap_drag = (not self.hold_toggle.get_active()
                         and (now - self.last_tap) / 1000.0 < DOUBLE_TAP_MS)
        if self.tap_drag:
            self.reports.set_buttons(self.reports.buttons | 1)
        else:
            self.long_press = GLib.timeout_add(LONG_PRESS_MS, self.on_long_press)

    def on_drag_update(self, _gesture, ox, oy):
        now = GLib.get_monotonic_time()
        dx, dy = ox - self.drag_last[0], oy - self.drag_last[1]
        dt = max((now - self.drag_time) / 1000.0, 1.0)
        self.drag_last, self.drag_time = (ox, oy), now
        if self.two_fingers or self.max_touches >= 3:
            # Scrolling or a three-finger gesture: no pointer motion
            return
        self.moved += abs(dx) + abs(dy)
        if self.moved > TAP_SLOP:
            self.cancel_long_press()
        # Pointer acceleration: slow movement stays precise, a quick swipe goes far. Not on
        # this phone's own desktop: phoc accelerates the virtual mouse itself, twice felt
        # sluggish when moving slowly
        gain = self.speed
        if self.local is None:
            speed = math.hypot(dx, dy) / dt
            gain *= 1.0 + ACCEL_GAIN * min(max(speed - ACCEL_THRESHOLD, 0.0), ACCEL_CAP)
        self.reports.move(dx * gain, dy * gain)

    def on_drag_end(self, _gesture, _ox, _oy):
        now = GLib.get_monotonic_time()
        duration = (now - self.touch_start) / 1000.0
        tap = (not self.two_fingers and self.max_touches < 3 and duration < TAP_MS
               and self.moved < TAP_SLOP)
        self.end_touch()
        if self.tap_drag:
            # Up again: the end of the drag, or the second click of a double click
            self.tap_drag = False
            self.reports.set_buttons(self.reports.buttons & ~1)
            self.last_tap = now if tap else 0
        elif tap:
            if self.hold_toggle.get_active():
                # A tap ends a held button instead of clicking
                self.hold_toggle.set_active(False)
            else:
                self.reports.click(1)
                self.last_tap = now

    def end_touch(self):
        self.cancel_long_press()
        self.pad.remove_css_class("touched")
        if self.holding:
            self.holding = False
            self.hold_toggle.set_active(False)

    def cancel_long_press(self):
        if self.long_press:
            GLib.source_remove(self.long_press)
            self.long_press = 0

    def on_long_press(self):
        """Finger held still: hold the left button until it is lifted, for dragging."""
        self.long_press = 0
        if not self.two_fingers and self.moved < TAP_SLOP:
            self.holding = True
            self.hold_toggle.set_active(True)
        return False

    def on_hold_toggled(self, toggle):
        if toggle.get_active():
            self.pad.add_css_class("holding")
            self.reports.set_buttons(self.reports.buttons | 1)
        else:
            self.pad.remove_css_class("holding")
            self.holding = False
            self.reports.set_buttons(self.reports.buttons & ~1)

    def on_two_begin(self, gesture, _sequence):
        self.two_fingers = True
        self.cancel_long_press()
        if self.tap_drag:
            self.tap_drag = False
            self.reports.set_buttons(self.reports.buttons & ~1)
        self.zoom_mode = None
        self.zoom_scale = 1.0
        ok, x, y = gesture.get_bounding_box_center()
        self.zoom_center = (x, y) if ok else None
        self.zoom_start = GLib.get_monotonic_time()
        self.zoom_moved = 0.0

    def on_two_update(self, gesture, _sequence):
        ok, x, y = gesture.get_bounding_box_center()
        if not ok:
            return
        if self.zoom_center is None:
            self.zoom_center = (x, y)
            return
        dx, dy = x - self.zoom_center[0], y - self.zoom_center[1]
        self.zoom_center = (x, y)
        self.zoom_moved += abs(dx) + abs(dy)
        if self.max_touches >= 3:
            return
        # Spreading or pinching the fingers zooms (Ctrl + wheel); moving them together scrolls.
        # Which one is decided once per touch, by what changes first.
        scale = gesture.get_scale_delta()
        if self.zoom_mode is None:
            if abs(scale - 1.0) > ZOOM_STEP:
                self.zoom_mode = "zoom"
            elif self.zoom_moved > TAP_SLOP:
                self.zoom_mode = "scroll"
            else:
                return
        if self.zoom_mode == "zoom":
            steps = int(math.log(scale / self.zoom_scale) / math.log(1.0 + ZOOM_STEP))
            if steps:
                self.zoom_scale *= (1.0 + ZOOM_STEP) ** steps
                self.reports.zoom(steps)
            return
        # Natural scrolling: the content follows the fingers like on the phone itself.
        # Wheel +1 scrolls up and AC Pan +1 scrolls right on the host.
        sign = 1.0 if self.natural else -1.0
        self.reports.scroll(sign * dy / SCROLL_STEP, -sign * dx / SCROLL_STEP)

    def on_two_end(self, _gesture, _sequence):
        duration = (GLib.get_monotonic_time() - self.zoom_start) / 1000.0
        self.zoom_center = None
        if (duration < TAP_MS and self.zoom_moved < TAP_SLOP and self.max_touches < 3
                and self.zoom_mode is None):
            self.reports.click(2)

    def on_pad_event(self, _controller, event):
        """Counts the fingers on the pad: a three-finger tap is the middle button, a
        three-finger swipe switches windows (left/right, Alt+Tab) or opens the overview (up,
        Super). The other gestures stay with GestureDrag and GestureZoom."""
        if event is None:
            return False                  # GTK passes none for some synthesized events
        kind = event.get_event_type()
        if kind not in (Gdk.EventType.TOUCH_BEGIN, Gdk.EventType.TOUCH_UPDATE,
                        Gdk.EventType.TOUCH_END, Gdk.EventType.TOUCH_CANCEL):
            return False
        sequence = event.get_event_sequence()
        ok, x, y = event.get_position()
        now = GLib.get_monotonic_time()
        if kind == Gdk.EventType.TOUCH_BEGIN:
            if not self.touches:
                self.max_touches = 0
                self.three_start = None
                self.three_switched = 0
                self.three_swiped = False
            self.touches[sequence] = (x, y)
            self.max_touches = max(self.max_touches, len(self.touches))
            if len(self.touches) == 3:
                self.three_start = (self.centroid(), now)
        elif kind == Gdk.EventType.TOUCH_UPDATE:
            if sequence in self.touches and ok:
                self.touches[sequence] = (x, y)
            if len(self.touches) == 3 and self.three_start:
                self.on_three_moved()
        else:
            self.touches.pop(sequence, None)
            if not self.touches:
                self.on_three_end(now)
        return False

    def centroid(self):
        xs = [p[0] for p in self.touches.values()]
        ys = [p[1] for p in self.touches.values()]
        return sum(xs) / len(xs), sum(ys) / len(ys)

    def on_three_moved(self):
        (x0, y0), _start = self.three_start
        x, y = self.centroid()
        dx, dy = x - x0, y - y0
        if abs(dx) >= abs(dy):
            # Alt stays down while the fingers are on the pad; each step is one more Tab
            steps = int(dx / SWIPE_STEP)
            while self.three_switched != steps:
                forward = steps > self.three_switched
                self.three_switched += 1 if forward else -1
                mods = MOD_ALT | (0 if forward else MOD_SHIFT)
                self.reports.keys(mods, [KEY_TAB])
                self.reports.keys(MOD_ALT, [])
                self.three_swiped = True
        elif dy < -SWIPE_STEP and not self.three_swiped:
            # Super alone: the overview on GNOME, the start menu on Windows
            self.reports.keys(MOD_SUPER, [])
            self.reports.keys(0, [])
            self.three_swiped = True

    def on_three_end(self, now):
        if self.max_touches < 3:
            return
        if self.three_switched:
            self.reports.keys(0, [])
        elif not self.three_swiped and self.three_start:
            (x0, y0), start = self.three_start
            if (now - start) / 1000.0 < TAP_MS * 1.5:
                self.reports.click(4)
        self.three_start = None

    # ----- Keyboard -----

    def take_modifiers(self):
        """The sticky modifiers that are on; they let go after this key."""
        mods = 0
        for bit, toggle in self.modifiers.items():
            if toggle.get_active():
                mods |= bit
                toggle.set_active(False)
        return mods

    def do_shutdown(self):
        if self.schwupp_watch:
            Gio.bus_unwatch_name(self.schwupp_watch)
            self.schwupp_watch = 0
        if self.local is not None:
            self.local.close()
            self.local = None
        if self.peripheral:
            self.peripheral.stop(sync=True)
        Adw.Application.do_shutdown(self)


def phone_monitor():
    """The phone's own screen (DSI, eDP, LVDS), not the TV's virtual one."""
    monitors = Gdk.Display.get_default().get_monitors()
    for i in range(monitors.get_n_items()):
        monitor = monitors.get_item(i)
        if (monitor.get_connector() or "").startswith(("DSI-", "eDP-", "LVDS-")):
            return monitor
    return None


def preload_layer_shell(argv, environ, execv=os.execve):
    """--desktop: start again with gtk4-layer-shell preloaded, once (see LAYER_SHELL_LIB)."""
    if "--desktop" not in argv or not os.path.exists(LAYER_SHELL_LIB):
        return
    preload = environ.get("LD_PRELOAD", "")
    if LAYER_SHELL_LIB in preload.split(":"):
        return
    env = dict(environ, LD_PRELOAD=":".join(filter(None, (LAYER_SHELL_LIB, preload))))
    execv(sys.executable, [sys.executable, os.path.abspath(argv[0]), *argv[1:]], env)


def main():
    preload_layer_shell(sys.argv, os.environ)
    app = RemoteApp(desktop="--desktop" in sys.argv)
    # Quit cleanly on SIGTERM/SIGINT so the adapter's Pairable setting is put back
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: (app.quit(), False)[1])
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
