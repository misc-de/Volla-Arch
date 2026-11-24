"""Shared by the session and the bar's menus of the desktop over VNC: settings (resolution, sound,
keyboard; the bar's edge in GSettings), the virtual screen, sound through a tunnel to the viewer's PC, the menus' windows."""
import configparser
import gettext
import json
import os
import subprocess
import time

_ = gettext.translation("plinius-desktop", fallback=True).gettext

DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "plinius-desktop")
SETTINGS = os.path.join(DIR, "settings.ini")
# Of plinius-desktop.service, gone when the session ends
RUNTIME_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "plinius-desktop")
CTL_SOCKET = os.path.join(RUNTIME_DIR, "wayvnc.sock")
# The virtual screen's name and the bar's file: outside RUNTIME_DIR, which systemd removes with
# the VNC session, while the desktop on a TV may still own the screen (plinius-desktop-cast)
SCREEN_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "plinius-desktop-screen")
OUTPUT_FILE = os.path.join(SCREEN_DIR, "output")
CLIENT_FILE = os.path.join(RUNTIME_DIR, "client")
PHONE_SINK = "hal_output"
DESKTOP_SINK = "plinius_desktop"
# The desktop on a TV: an output of its own the phone does not play; Schwupp sends what reaches it
TV_SINK = "plinius_tv"

RESOLUTIONS = [("1280x720", "1280 × 720"),
               ("1600x900", "1600 × 900"), ("1920x1080", "1920 × 1080"),
               ("1920x1200", "1920 × 1200"), ("2560x1440", "2560 × 1440"),
               ("2560x1600", "2560 × 1600"), ("3840x2160", "3840 × 2160"),
               ("3840x2400", "3840 × 2400")]
SCALES = [("1", "100 %"), ("1.25", "125 %"), ("1.5", "150 %"), ("1.75", "175 %"), ("2", "200 %")]
LAYOUTS = [("us", _("English (US)")), ("gb", _("English (UK)")), ("de", _("German")),
           ("ch", _("German (Switzerland)")), ("fr", _("French")), ("es", _("Spanish")),
           ("it", _("Italian")), ("nl", _("Dutch")), ("pl", _("Polish")), ("pt", _("Portuguese")),
           ("ru", _("Russian"))]
DEFAULTS = {"resolution": "1920x1080", "scale": "1.5", "sound": "phone"}


def system_keyboard():
    """The first layout of the phone's settings, as wayvnc writes it ("de-nodeadkeys"); else US."""
    from gi.repository import Gio
    schema = "org.gnome.desktop.input-sources"
    try:
        for kind, layout in Gio.Settings.new(schema).get_value("sources").unpack():
            if kind == "xkb" and layout:
                return layout.replace("+", "-")
    except Exception as error:  # A missing schema aborts in GLib before this; anything else is no reason to stop
        print("input sources:", error, flush=True)
    return "us"


def load():
    parser = configparser.ConfigParser()
    parser.read(SETTINGS)
    values = dict(DEFAULTS)
    if parser.has_section("desktop"):
        values.update(parser["desktop"])
    if "keyboard" not in values:
        values["keyboard"] = system_keyboard()
    return values


def save(values):
    parser = configparser.ConfigParser()
    parser["desktop"] = values
    os.makedirs(DIR, exist_ok=True)
    with open(SETTINGS, "w") as f:
        parser.write(f)


# The bar's edge: GNOME Settings › Remote Access sets it, the session moves the bar when it changes
SCHEMA = "org.plinius.desktop"
BAR_POSITIONS = ("top", "bottom")


def bar_position():
    """ "top" or "bottom"; top without the schema (as its default)."""
    from gi.repository import Gio
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup(SCHEMA, True) is None:
        return "top"
    value = Gio.Settings.new(SCHEMA).get_string("bar-position")
    return value if value in BAR_POSITIONS else "top"


def anchor_to_bar(window, side):
    """Puts a menu window at the bar's end on side (GtkLayerShell.Edge.LEFT or RIGHT), at the
    edge the bar is on, with the same gaps whichever edge that is."""
    from gi.repository import GtkLayerShell
    edge = GtkLayerShell.Edge.TOP if bar_position() == "top" else GtkLayerShell.Edge.BOTTOM
    GtkLayerShell.set_anchor(window, side, True)
    GtkLayerShell.set_anchor(window, edge, True)
    GtkLayerShell.set_margin(window, side, 8)
    GtkLayerShell.set_margin(window, edge, 6)


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(" ".join(cmd), "->", result.returncode, result.stderr.strip(), flush=True)
    return result


def virtual_output():
    """The name of the virtual screen, None when there is none."""
    try:
        with open(OUTPUT_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def set_resolution(resolution):
    """The size of the virtual screen."""
    output = virtual_output()
    if output:
        run(["wlr-randr", "--output", output, "--custom-mode", resolution])


def plain_address(address):
    """An IPv4 viewer on the dual-stack socket shows as ::ffff:10.15.19.111."""
    return address.removeprefix("::ffff:") if address else address


def client_address():
    out = run(["wayvncctl", "-S", CTL_SOCKET, "-j", "client-list"]).stdout
    try:
        clients = json.loads(out)
    except ValueError:
        return None
    return plain_address(clients[0].get("address")) if clients else None


def sinks():
    return run(["pactl", "list", "sinks", "short"]).stdout


def set_sound(target):
    """Default sink and every playing stream to the phone, through a tunnel to the viewer's PC
    or, with the desktop on a TV, to the TV's sink (the phone stays silent)."""
    sink = PHONE_SINK
    if target == "desktop":
        current = sinks()
        sink = DESKTOP_SINK
        if DESKTOP_SINK not in current:
            address = client_address()
            if address:
                run(["pactl", "load-module", "module-tunnel-sink",
                     f"server=tcp:{address}:4713", f"sink_name={DESKTOP_SINK}",
                     "sink_properties=device.description=Desktop"])
            elif TV_SINK in current:
                sink = TV_SINK
            else:
                return False
    run(["pactl", "set-default-sink", sink])
    for line in run(["pactl", "list", "sink-inputs", "short"]).stdout.splitlines():
        run(["pactl", "move-sink-input", line.split()[0], sink])
    return True


def drop_desktop_sound():
    """Back to the phone and the tunnel gone, once the session ended."""
    set_sound("phone")
    for line in run(["pactl", "list", "modules", "short"]).stdout.splitlines():
        if "module-tunnel-sink" in line and DESKTOP_SINK in line:
            run(["pactl", "unload-module", line.split()[0]])


def make_tv_sink():
    """The TV's sink, while the desktop is on a TV (a null sink: only its monitor is heard)."""
    if TV_SINK not in sinks():
        run(["pactl", "load-module", "module-null-sink", f"sink_name={TV_SINK}",
             "sink_properties=device.description=TV"])


def drop_tv_sink():
    """Back to the phone and the TV's sink gone, once the desktop left the TV."""
    set_sound("phone")
    for line in run(["pactl", "list", "modules", "short"]).stdout.splitlines():
        fields = line.split("\t")
        if len(fields) > 2 and fields[1] == "module-null-sink" \
                and f"sink_name={TV_SINK}" in fields[2].split():
            run(["pactl", "unload-module", fields[0]])


def screen_locked(bus):
    """Whether the lock screen is up; a lock screen that does not answer counts as not."""
    from gi.repository import Gio, GLib
    try:
        return bus.call_sync("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
                             "org.gnome.ScreenSaver", "GetActive", None,
                             GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE,
                             1000, None).unpack()[0]
    except GLib.Error:
        return False


def close_when_locked(quit):
    """Menus never show above the lock screen: quit now if locked, and once it locks.

    Returns False when the screen is locked already.
    """
    from gi.repository import Gio
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def on_signal(_bus, _sender, _path, _iface, _signal, params):
        if params.unpack()[0]:
            quit()

    # Before the question, so a lock right after it is not missed
    bus.signal_subscribe("org.gnome.ScreenSaver", "org.gnome.ScreenSaver", "ActiveChanged",
                         "/org/gnome/ScreenSaver", None, Gio.DBusSignalFlags.NONE, on_signal)
    close_when_locked.bus = bus  # keep the connection
    return not screen_locked(bus)


def scope_name(app_id, suffix):
    """Unit name of an app's own scope, escaped the way systemd-escape does it."""
    if app_id.endswith(".desktop"):
        app_id = app_id[:-len(".desktop")]
    safe = "".join(c if c.isascii() and (c.isalnum() or c in ":_.")
                   else "".join(f"\\x{b:02x}" for b in c.encode()) for c in app_id)
    return f"app-desktop-{safe or 'app'}-{suffix}.scope"


def start_in_scope(app_id, argv, env=None):
    """Starts a program from the desktop in a scope of its own, as Phosh does with the apps it
    starts: inside plinius-desktop.service, the session's end took the program with it, and so
    did the kernel's OOM killer ending any one of its processes; and a user service is no app to
    plinius-appaccess, which allows it everything. systemd-run moves itself into the scope and
    only then runs the program, so not one of its processes starts in the service. Moving the
    program after it was started (StartTransientUnit with its pid) left every child it made
    before behind. Returns the process, None if it could not be started."""
    # Digits only, as systemd's own scopes: the part after the last dash is no part of the app id
    unit = scope_name(app_id, f"{os.getpid()}{time.monotonic_ns()}")
    try:
        return subprocess.Popen(["systemd-run", "--user", "--scope", "--collect", "--quiet",
                                 f"--unit={unit}", "--", *argv], env=env)
    except OSError as e:
        print("launch failed:", e, flush=True)
        return None


MENU_PIDFILES = ["plinius-desk-apps.pid", "plinius-desk-settings.pid", "plinius-desk-network.pid",
                 "plinius-desk-battery.pid"]
# The app menu that waits hidden for the whole session (desk-apps.py --resident): SIGUSR1 shows
# or hides it, SIGUSR2 only hides it, SIGTERM ends it
RESIDENT_APPS_PIDFILE = "plinius-desk-apps.resident"


def close_all_menus():
    """The session ends: no menu stays behind."""
    close_other_menus(None)


def close_other_menus(own):
    """Only one menu at a time: opening one closes the others."""
    import signal
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    for name in MENU_PIDFILES:
        if name != own:
            signal_menu(os.path.join(runtime, name), b"desk-", signal.SIGTERM)
    if own != "plinius-desk-apps.pid":
        # Hidden, not ended: it stays ready for the next click
        signal_menu(os.path.join(runtime, RESIDENT_APPS_PIDFILE), b"--resident", signal.SIGUSR2)


def signal_menu(pidfile, marker, signum):
    """Sends signum to the menu in pidfile if that process still is one (marker in its
    command line): a pid left behind may belong to another program by now."""
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            if marker in f.read():
                os.kill(pid, signum)
    except (OSError, ValueError):
        pass


def click_catcher(monitor, on_click):
    """An invisible layer over the screen above the bar: a click next to the menu closes it.

    Map it before the menu so the menu stays above it. It keeps clear of the bar
    (exclusive zone 0 respects the bar's), so the bar's buttons still work.
    """
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import Gdk, Gtk, GtkLayerShell

    window = Gtk.Window()
    GtkLayerShell.init_for_window(window)
    GtkLayerShell.set_namespace(window, "plinius-desk-catcher")
    GtkLayerShell.set_layer(window, GtkLayerShell.Layer.OVERLAY)
    for edge in (GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT,
                 GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM):
        GtkLayerShell.set_anchor(window, edge, True)
    GtkLayerShell.set_exclusive_zone(window, 0)
    GtkLayerShell.set_keyboard_mode(window, GtkLayerShell.KeyboardMode.NONE)
    GtkLayerShell.set_monitor(window, monitor)
    visual = window.get_screen().get_rgba_visual()
    if visual:
        window.set_visual(visual)
    window.set_app_paintable(True)

    def draw(widget, cr):
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(1)  # cairo.OPERATOR_SOURCE
        cr.paint()
        return True

    window.connect("draw", draw)
    window.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.TOUCH_MASK)
    window.connect("button-press-event", lambda *a: on_click() or True)
    window.connect("touch-event", lambda *a: on_click() or True)
    window.show_all()
    return window


def outputs():
    """{name: {"enabled": bool, "position": "x,y"}} from wlr-randr: an output's lines are
    indented below its name, the next unindented line starts the next output."""
    result, current = {}, None
    out = subprocess.run(["wlr-randr"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line and not line[0].isspace():
            current = line.split()[0]
            result[current] = {"enabled": False, "position": None, "mode": None, "scale": None}
        elif current:
            text = line.strip()
            if text.endswith("(current)") and " px" in text:
                result[current]["mode"] = text.split(" px", 1)[0]
            elif text.startswith("Scale:"):
                result[current]["scale"] = float(text.split(":", 1)[1])
            elif text.startswith("Enabled:"):
                result[current]["enabled"] = text.split(":", 1)[1].strip() == "yes"
            elif text.startswith("Position:"):
                result[current]["position"] = text.split(":", 1)[1].strip()
    return result


INFO_CSS = b"""
window.desk-info { background: transparent; }
.desk-card {
  background-color: rgba(20, 22, 28, 0.94);
  border: 1px solid rgba(255, 255, 255, 0.10);
  border-radius: 16px;
  padding: 18px 20px;
  color: #eeeeee;
}
.desk-title { font-size: 16px; font-weight: bold; }
.desk-key { opacity: 0.65; }
"""


def virtual_monitor(display):
    """The virtual screen is the one without a physical size."""
    for i in range(display.get_n_monitors()):
        monitor = display.get_monitor(i)
        if monitor.get_width_mm() == 0:
            return monitor
    return None


def info_popup(pidname, title, rows, button_label, button_cmd):
    """A card with details at the bar's right end: rows() gives [(label, value)] or a
    text; a click on the icon again, next to the card or another menu closes it."""
    import signal
    import gi
    gi.require_version("Gdk", "3.0")
    gi.require_version("Gtk", "3.0")
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import Gdk, GLib, Gtk, GtkLayerShell

    pidfile = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), pidname)
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            if b"desk-" in f.read():
                os.kill(pid, signal.SIGTERM)
                return
    except (OSError, ValueError):
        pass
    if not close_when_locked(Gtk.main_quit):
        return
    monitor = virtual_monitor(Gdk.Display.get_default())
    if monitor is None:
        return
    close_other_menus(pidname)
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))

    Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
    provider = Gtk.CssProvider()
    provider.load_from_data(INFO_CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    click_catcher(monitor, Gtk.main_quit)

    window = Gtk.Window()
    window.get_style_context().add_class("desk-info")
    GtkLayerShell.init_for_window(window)
    GtkLayerShell.set_namespace(window, "plinius-desk-info")
    GtkLayerShell.set_layer(window, GtkLayerShell.Layer.OVERLAY)
    anchor_to_bar(window, GtkLayerShell.Edge.RIGHT)
    GtkLayerShell.set_monitor(window, monitor)
    visual = window.get_screen().get_rgba_visual()
    if visual:
        window.set_visual(visual)
    window.set_app_paintable(True)

    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    card.get_style_context().add_class("desk-card")
    card.set_size_request(320, -1)
    window.add(card)
    label = Gtk.Label(label=title, xalign=0)
    label.get_style_context().add_class("desk-title")
    card.pack_start(label, False, False, 0)

    content = rows()
    if isinstance(content, str):
        card.pack_start(Gtk.Label(label=content, xalign=0), False, False, 0)
    else:
        grid = Gtk.Grid(column_spacing=16, row_spacing=6)
        for i, (key, value) in enumerate(content):
            k = Gtk.Label(label=key, xalign=0)
            k.get_style_context().add_class("desk-key")
            v = Gtk.Label(label=value, xalign=0)
            v.set_line_wrap(True)
            grid.attach(k, 0, i, 1, 1)
            grid.attach(v, 1, i, 1, 1)
        card.pack_start(grid, False, False, 0)

    def on_button(_button):
        start_in_scope(os.path.basename(button_cmd[0]), button_cmd)
        Gtk.main_quit()

    button = Gtk.Button(label=button_label)
    button.connect("clicked", on_button)
    card.pack_start(button, False, False, 4)

    window.connect("destroy", Gtk.main_quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, Gtk.main_quit)
    window.show_all()
    Gtk.main()
    try:
        os.unlink(pidfile)
    except OSError:
        pass
