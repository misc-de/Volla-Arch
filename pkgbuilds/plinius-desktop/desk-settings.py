#!/usr/bin/python3
"""Settings menu of the virtual desktop, at the bar's gear.

Resolution of the virtual screen, sound on the phone or the desktop, keyboard
layout of the VNC keyboard. A second click on the gear closes it.
"""
import os
import signal
import subprocess
import sys

import gi
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, GLib, Gtk, GtkLayerShell  # noqa: E402

import desk_common as common  # noqa: E402

_ = common._

PIDFILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "plinius-desk-settings.pid")

CSS = b"""
window.desk-settings { background: transparent; }
.desk-card {
  background-color: rgba(20, 22, 28, 0.94);
  border: 1px solid rgba(255, 255, 255, 0.10);
  border-radius: 16px;
  padding: 18px 20px;
  color: #eeeeee;
}
.desk-title { font-size: 16px; font-weight: bold; }
.desk-label { opacity: 0.75; font-size: 12px; }
.desk-note { opacity: 0.6; font-size: 11px; }
.desk-card button.toggle:checked { background: #1793d1; color: #ffffff; }
"""


def toggle_running():
    """Close an open menu instead of opening a second one."""
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            if b"desk-settings" in f.read():
                os.kill(pid, signal.SIGTERM)
                return True
    except (OSError, ValueError):
        pass
    return False


def virtual_monitor(display):
    """The virtual screen is the one without a physical size."""
    for i in range(display.get_n_monitors()):
        monitor = display.get_monitor(i)
        if monitor.get_width_mm() == 0:
            return monitor
    return None


class Menu(Gtk.Window):
    def __init__(self):
        super().__init__()
        self.settings = common.load()
        self.get_style_context().add_class("desk-settings")
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "plinius-desk-settings")
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        # At the bar's right end, on the edge the bar is on
        common.anchor_to_bar(self, GtkLayerShell.Edge.RIGHT)
        GtkLayerShell.set_monitor(self, virtual_monitor(self.get_display()))

        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class("desk-card")
        card.set_size_request(320, -1)
        self.add(card)

        title = Gtk.Label(label=_("Desktop"), xalign=0)
        title.get_style_context().add_class("desk-title")
        card.pack_start(title, False, False, 0)

        card.pack_start(self.label(_("Resolution")), False, False, 4)
        resolution = Gtk.ComboBoxText()
        for key, text in common.RESOLUTIONS:
            resolution.append(key, text)
        resolution.set_active_id(self.settings["resolution"])
        resolution.connect("changed", self.on_resolution)
        card.pack_start(resolution, False, False, 0)

        card.pack_start(self.label(_("Scale")), False, False, 4)
        scale = Gtk.ComboBoxText()
        for key, text in common.SCALES:
            scale.append(key, text)
        scale.set_active_id(self.settings["scale"])
        scale.connect("changed", self.on_scale)
        card.pack_start(scale, False, False, 0)

        card.pack_start(self.label(_("Sound")), False, False, 4)
        sound = Gtk.Box(spacing=0)
        sound.get_style_context().add_class("linked")
        self.sound_phone = Gtk.ToggleButton(label=_("Phone"))
        self.sound_desktop = Gtk.ToggleButton(label=_("Desktop"))
        for button in (self.sound_phone, self.sound_desktop):
            button.get_style_context().add_class("toggle")
            sound.pack_start(button, True, True, 0)
        self.sound_phone.set_active(self.settings["sound"] != "desktop")
        self.sound_desktop.set_active(self.settings["sound"] == "desktop")
        self.sound_phone.connect("toggled", self.on_sound, "phone")
        self.sound_desktop.connect("toggled", self.on_sound, "desktop")
        card.pack_start(sound, False, False, 0)

        card.pack_start(self.label(_("Keyboard")), False, False, 4)
        keyboard = Gtk.ComboBoxText()
        for key, text in common.LAYOUTS:
            keyboard.append(key, text)
        # The phone's own layout may be one not in the list
        if self.settings["keyboard"] not in dict(common.LAYOUTS):
            keyboard.append(self.settings["keyboard"], self.settings["keyboard"])
        keyboard.set_active_id(self.settings["keyboard"])
        keyboard.connect("changed", self.on_keyboard)
        card.pack_start(keyboard, False, False, 0)
        self.keyboard_note = Gtk.Box(spacing=8)
        note = Gtk.Label(label=_("Applies after reconnecting"), xalign=0)
        note.get_style_context().add_class("desk-note")
        reconnect = Gtk.Button(label=_("Reconnect now"))
        reconnect.connect("clicked", self.on_reconnect)
        self.keyboard_note.pack_start(note, True, True, 0)
        self.keyboard_note.pack_start(reconnect, False, False, 0)
        self.keyboard_note.set_no_show_all(True)
        card.pack_start(self.keyboard_note, False, False, 0)

    @staticmethod
    def label(text):
        label = Gtk.Label(label=text, xalign=0)
        label.get_style_context().add_class("desk-label")
        return label

    def store(self, key, value):
        self.settings[key] = value
        common.save(self.settings)

    def on_resolution(self, combo):
        key = combo.get_active_id()
        self.store("resolution", key)
        common.set_resolution(key)

    def on_scale(self, combo):
        # plinius-desktop-session applies it, together with the viewer's window size
        self.store("scale", combo.get_active_id())

    def on_sound(self, button, target):
        if not button.get_active():
            # One of the two stays pressed
            other = self.sound_desktop if target == "phone" else self.sound_phone
            if not other.get_active():
                button.set_active(True)
            return
        other = self.sound_desktop if target == "phone" else self.sound_phone
        other.set_active(False)
        if common.set_sound(target):
            self.store("sound", target)

    def on_keyboard(self, combo):
        self.store("keyboard", combo.get_active_id())
        self.keyboard_note.set_no_show_all(False)
        self.keyboard_note.show_all()

    def on_reconnect(self, button):
        # The viewer has to connect again, wayvnc reads the layout when it starts
        subprocess.Popen(["systemctl", "--user", "restart", "plinius-desktop.service"])
        Gtk.main_quit()


def main():
    if toggle_running():
        return
    if not common.close_when_locked(Gtk.main_quit):
        return
    # Only on the virtual screen, never on the phone (e.g. VNC just ended)
    monitor = virtual_monitor(Gdk.Display.get_default())
    if monitor is None:
        return
    common.close_other_menus("plinius-desk-settings.pid")
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    # Dark like the bar, also for the drop-downs
    Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    # A click next to the menu closes it
    common.click_catcher(monitor, Gtk.main_quit)
    menu = Menu()
    menu.connect("destroy", Gtk.main_quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, Gtk.main_quit)
    menu.show_all()
    Gtk.main()
    try:
        os.unlink(PIDFILE)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
