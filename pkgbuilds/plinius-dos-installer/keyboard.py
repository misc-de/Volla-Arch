"""The on-screen keyboard (Stevia) for DOSBox: unfolded by itself, in the Terminal layout.

DOSBox 0.74 (SDL 1.2, under Xwayland) asks for no input method, so Stevia never unfolds for it,
and only Stevia's Terminal layout sends keycodes SDL 1.2 understands. Stevia keeps a layout per
application (mobi.phosh.osk.application, by app id) and applies it whenever that application's
window becomes active: DOSBox gets the Terminal layout there. The keyboard itself is unfolded
over D-Bus once DOSBox's window is up, and folded again when DOSBox ends.
"""

import threading

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

# The app id of DOSBox's window, set through SDL's environment (dosbox.ENV)
APP_ID = "dosbox"
SCHEMA = "mobi.phosh.osk.application"
OSK = ("sm.puri.OSK0", "/sm/puri/OSK0", "sm.puri.OSK0")
# DOSBox needs a moment until its window is there and active; one more try after that, in case
# it took longer (a keyboard folded away by hand later stays folded)
SHOW_AFTER = 1.5
RETRY_AFTER = 3.0


def use_terminal_layout(log=print):
    source = Gio.SettingsSchemaSource.get_default()
    if not source or not source.lookup(SCHEMA, True):
        return
    settings = Gio.Settings.new_with_path(SCHEMA, f"/mobi/phosh/osk/application/{APP_ID}/")
    if settings.get_value("override-layout").unpack()[0] != "terminal":
        settings.set_value("override-layout", GLib.Variant("(ss)", ("terminal", "")))
        Gio.Settings.sync()
        log("keyboard: Terminal layout for DOSBox")


def _call(method, args, reply=None):
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return bus.call_sync(*OSK[:2], method[0], method[1], args, reply,
                             Gio.DBusCallFlags.NONE, 2000, None)
    except GLib.Error:
        return None


def set_visible(visible):
    return _call((OSK[2], "SetVisible"), GLib.Variant("(b)", (visible,))) is not None


def is_visible():
    r = _call(("org.freedesktop.DBus.Properties", "Get"), GLib.Variant("(ss)", (OSK[2], "Visible")),
              GLib.VariantType("(v)"))
    return bool(r and r.unpack()[0])


class ForDosbox:
    """with ForDosbox(log): the keyboard is unfolded while the block runs DOSBox."""

    def __init__(self, log=print):
        self.log = log
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        try:
            use_terminal_layout(self.log)
        except GLib.Error as e:
            self.log(f"keyboard: {e.message}")
        self._thread = threading.Thread(target=self._show, name="keyboard", daemon=True)
        self._thread.start()
        return self

    def _show(self):
        if self._stop.wait(SHOW_AFTER):
            return
        if not set_visible(True):
            self.log("keyboard: no on-screen keyboard on the session bus")
            return
        if not self._stop.wait(RETRY_AFTER) and not is_visible():
            set_visible(True)

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=1)
        set_visible(False)
        return False
