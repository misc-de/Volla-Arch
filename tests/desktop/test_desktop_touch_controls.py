# plinius-touch-controls: the keys it holds, the Wayland wire protocol against a fake compositor
# on a real socket pair (registry, virtual keyboard with its keymap file, foreign toplevels),
# where the pads go upright and lying, the stick and the buttons, the drawing on a real cairo
# surface, the pads and the overlay with GTK faked, and the program from start to end.

import array
import math
import os
import re
import select
import socket
import struct
import types

import cairo
import pytest

import desktop_fakes as fakes
from conftest import PKG, load_script
from desktop_mainrun import run_as_main

TOUCH = PKG / "plinius-touch-controls" / "plinius-touch-controls.py"


@pytest.fixture(scope="module")
def mod():
    return fakes.load_gui(TOUCH, "plinius_touch_controls_under_test")


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    """The chosen style is kept under XDG_STATE_HOME: a temporary one, never the real home."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state"


# ----- a compositor on the other end of a socket pair -----

def u32(*v):
    return struct.pack(f"<{len(v)}I", *v)


def wl_string(text):
    data = text.encode() + b"\0"
    return u32(len(data)) + data + b"\0" * (-len(data) % 4)


def wl_array(values):
    return u32(4 * len(values)) + u32(*values)


class Compositor:
    """The server side: events written for the client, the client's requests read back."""

    GLOBALS = (("wl_seat", 7), ("zwp_virtual_keyboard_manager_v1", 1),
               ("zwlr_foreign_toplevel_manager_v1", 3), ("wl_compositor", 6))

    def __init__(self):
        self.server, self.client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.buffer = b""
        self.fds = []

    def event(self, oid, opcode, payload=b""):
        self.server.sendall(u32(oid, (8 + len(payload)) << 16 | opcode) + payload)

    def announce(self, globals_=GLOBALS, registry=2, callback=3):
        """What the compositor answers to get_registry and the first sync."""
        for n, (interface, version) in enumerate(globals_, 1):
            self.event(registry, 0, u32(n) + wl_string(interface) + u32(version))
        self.event(callback, 0, u32(0))

    def requests(self):
        """Everything the client sent so far: [(object, opcode, payload, fds)]."""
        out = []
        while select.select([self.server], [], [], 0)[0]:
            data, anc, _f, _a = self.server.recvmsg(65536, socket.CMSG_SPACE(16 * 4))
            if not data:
                break
            for level, kind, raw in anc:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    self.fds += list(array.array("i", raw[:len(raw) - len(raw) % 4]))
            self.buffer += data
        while len(self.buffer) >= 8:
            oid, word = struct.unpack_from("<II", self.buffer)
            size = word >> 16
            out.append((oid, word & 0xffff, self.buffer[8:size]))
            self.buffer = self.buffer[size:]
        return out

    def close(self):
        for s in (self.server, self.client):
            s.close()
        for fd in self.fds:
            os.close(fd)


@pytest.fixture
def comp():
    c = Compositor()
    yield c
    c.close()


def connected(mod, comp, globals_=Compositor.GLOBALS):
    comp.announce(globals_)
    wire = mod.Wire(comp.client)
    return wire, mod.Registry(wire)


# ----- keys -----

class Board:
    def __init__(self):
        self.calls = []

    def key(self, code, pressed):
        self.calls.append(("key", code, pressed))

    def modifiers(self, mask):
        self.calls.append(("mods", mask))


def test_a_key_held_by_two_controls_goes_up_with_the_last(mod):
    board = Board()
    keys = mod.Keys(board)
    keys.press(57)
    keys.press(57)
    keys.release(57)
    assert board.calls == [("key", 57, True)]
    keys.release(57)
    keys.release(57)  # not held: nothing
    assert board.calls == [("key", 57, True), ("key", 57, False)]


def test_modifiers_follow_ctrl_alt_and_shift(mod):
    board = Board()
    keys = mod.Keys(board)
    keys.press(29)
    keys.press(56)
    keys.press(103)
    keys.release(29)
    keys.press(42)
    keys.release_all()
    assert board.calls == [("key", 29, True), ("mods", 4), ("key", 56, True), ("mods", 12),
                           ("key", 103, True), ("key", 29, False), ("mods", 8),
                           ("key", 42, True), ("mods", 9),
                           ("key", 56, False), ("mods", 1), ("key", 103, False),
                           ("key", 42, False), ("mods", 0)]
    assert keys.count == {}


def test_key_table(mod):
    keys = mod.KEYS
    assert keys["f1"][0] == 59 and keys["f10"][0] == 68 and keys["f11"][0] == 87
    assert keys["f12"] == (88, "F12")
    assert keys["1"][0] == 2 and keys["9"][0] == 10 and keys["0"][0] == 11
    assert keys["q"] == (16, "Q") and keys["m"][0] == 50 and keys["a"][0] == 30
    for stick in mod.STICKS.values():
        assert all(name in keys for name in stick)
    assert all(n in keys for n in mod.DEFAULT_BUTTONS)
    assert all(n in keys or n in mod.ACTIONS for n in mod.DEFAULT_SMALL)
    # Mortal Kombat's number pad, Wolfenstein's Shift, Hexen's jump, the inventory
    assert [keys[f"kp{n}"][0] for n in (7, 8, 9, 4, 5, 6, 1, 2, 3)] == [
        71, 72, 73, 75, 76, 77, 79, 80, 81]
    assert keys["rshift"] == (54, "Shift") and keys["shift"][0] == 42
    assert keys["slash"] == (53, "/") and keys["lbracket"] == (26, "[")
    assert keys["rbracket"] == (27, "]")
    # The right Shift is a Shift for the modifiers too
    assert mod.MODIFIERS[keys["rshift"][0]] == mod.MODIFIERS[keys["shift"][0]]
    # Names as a preset of the DOS Installer may write them
    assert all(re.fullmatch(r"[a-z0-9]{1,10}", n) for n in list(keys) + list(mod.ACTIONS))


def test_the_dos_installers_games_get_keys_the_controls_have(mod):
    """Each game's controls in the DOS Installer (presets.TOUCH_GAMES) fit: known keys, no list
    longer than the controls take, the same limits."""
    presets = load_script(PKG / "plinius-dos-installer" / "presets.py", "presets_for_touch")
    assert presets.TOUCH_LISTS == {"buttons": mod.MAX_BUTTONS, "small": mod.MAX_SMALL,
                                   "keys": mod.MAX_KEYS}
    assert set(presets.TOUCH_STICKS) == set(mod.STICKS)
    for program, layout in presets.TOUCH_GAMES.items():
        args = mod.parse_args(presets.touch_args({}, program))
        assert args.buttons == layout["buttons"], program
        assert args.small == layout["small"], program
        assert args.keys == layout.get("keys", []), program


# ----- the wire protocol -----

def test_registry_and_virtual_keyboard_with_its_keymap(mod, comp, monkeypatch):
    wire, registry = connected(mod, comp)
    assert registry.globals["wl_seat"] == (1, 7)
    assert comp.requests() == [(1, 1, u32(2)), (1, 0, u32(3))]
    # The callback is done and gone
    assert 3 not in wire.objects
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(monotonic=lambda: 12.3456))
    vk = mod.VirtualKeyboard(registry)
    vk.key(57, True)
    vk.key(57, False)
    vk.modifiers(4)
    reqs = comp.requests()
    assert reqs[0] == (2, 0, u32(1) + wl_string("wl_seat") + u32(1, 4))
    assert reqs[1] == (2, 0, u32(2) + wl_string("zwp_virtual_keyboard_manager_v1") + u32(1, 5))
    assert reqs[2] == (5, 0, u32(4, 6))
    keymap = mod.KEYMAP.encode() + b"\0"
    assert reqs[3] == (6, 0, u32(1, len(keymap)))
    assert reqs[4:] == [(6, 1, u32(12345, 57, 1)), (6, 1, u32(12345, 57, 0)),
                        (6, 2, u32(4, 0, 0, 0))]
    # The keymap came as a file of its own
    assert len(comp.fds) == 1
    assert os.pread(comp.fds[0], len(keymap) + 10, 0) == keymap


def test_bind_takes_the_lower_version_and_refuses_missing_globals(mod, comp):
    wire, registry = connected(mod, comp, (("wl_seat", 1),))
    comp.requests()
    oid = registry.bind("wl_seat", 5)
    assert comp.requests() == [(2, 0, u32(1) + wl_string("wl_seat") + u32(1, oid))]
    with pytest.raises(mod.WaylandError, match="no zwp_virtual_keyboard_manager_v1"):
        mod.VirtualKeyboard(registry)


def test_globals_that_go_away(mod, comp):
    wire, registry = connected(mod, comp)
    comp.event(2, 1, u32(3))
    comp.event(2, 7)  # an opcode of a later version
    assert wire.read()
    assert "zwlr_foreign_toplevel_manager_v1" not in registry.globals
    assert "wl_seat" in registry.globals


def test_events_split_across_reads_and_for_unknown_objects(mod, comp):
    wire = mod.Wire(comp.client)
    got = []
    oid = wire.new_id(lambda op, body: got.append((op, body)))
    message = u32(oid, 16 << 16 | 3) + u32(7, 8)
    comp.server.sendall(message[:10])
    assert wire.read() and got == []
    comp.server.sendall(message[10:] + u32(99, 8 << 16))
    assert wire.read() and got == [(3, u32(7, 8))]
    # Nothing to read: still connected
    assert wire.read()


def test_a_broken_message_is_an_error(mod, comp):
    wire = mod.Wire(comp.client)
    comp.server.sendall(u32(1, 4 << 16))
    with pytest.raises(mod.WaylandError, match="broken message"):
        wire.read()


def test_compositor_errors_and_deleted_ids(mod, comp):
    wire = mod.Wire(comp.client)
    oid = wire.new_id()
    comp.event(1, 1, u32(oid))
    assert wire.read() and oid not in wire.objects
    comp.event(1, 0, u32(5, 2) + wl_string("invalid keymap"))
    with pytest.raises(mod.WaylandError, match="error 2 on object 5: invalid keymap"):
        wire.read()


def test_hang_up_and_files_the_compositor_sends(mod, comp):
    wire = mod.Wire(comp.client)
    r, w = os.pipe()
    os.close(w)
    comp.server.sendmsg([u32(77, 8 << 16)], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                               array.array("i", [r]).tobytes())])
    os.close(r)
    before = set(os.listdir("/proc/self/fd"))
    assert wire.read()
    # The file that came along is closed again
    assert len(set(os.listdir("/proc/self/fd")) - before) == 0
    comp.server.shutdown(socket.SHUT_WR)
    assert wire.read() is False


def test_read_on_a_closed_socket(mod):
    a, b = socket.socketpair()
    b.close()
    wire = mod.Wire(a)
    a.close()
    assert wire.read() is False


def test_roundtrip_without_an_answer(mod, comp, monkeypatch):
    wire = mod.Wire(comp.client)
    with pytest.raises(mod.WaylandError, match="does not answer"):
        wire.roundtrip(timeout=0.01)
    comp.server.shutdown(socket.SHUT_WR)
    with pytest.raises(mod.WaylandError, match="hung up"):
        wire.roundtrip(timeout=1)


def test_a_short_send_is_finished(mod):
    class Sock:
        def __init__(self):
            self.out = b""

        def sendmsg(self, parts, anc):
            self.out += parts[0][:5]
            return 5

        def sendall(self, data):
            self.out += data
    sock = Sock()
    mod.Wire(sock).send(3, 1, u32(9))
    assert sock.out == u32(3, 12 << 16 | 1, 9)


def test_short_events(mod):
    with pytest.raises(mod.WaylandError, match="short event"):
        mod.Reader(b"\1\0").uint()
    with pytest.raises(mod.WaylandError, match="short event"):
        mod.Reader(u32(12) + b"abc").blob()
    r = mod.Reader(wl_string("dosbox") + wl_array([1, 2]) + u32(5) + b"\1\0\0\0\2")
    assert r.string() == "dosbox" and r.uints() == [1, 2] and r.uints() == [1]


def test_connect_to_the_session_socket(mod, tmp_path):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(tmp_path / "wayland-1"))
    server.listen(2)
    try:
        wire = mod.Wire.connect({"XDG_RUNTIME_DIR": str(tmp_path), "WAYLAND_DISPLAY": "wayland-1"})
        wire.sock.close()
        wire = mod.Wire.connect({"WAYLAND_DISPLAY": str(tmp_path / "wayland-1")})
        wire.sock.close()
        with pytest.raises(OSError):
            mod.Wire.connect({"XDG_RUNTIME_DIR": str(tmp_path)})  # wayland-0 is not there
    finally:
        server.close()


def test_connect_from_the_environment(mod, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("WAYLAND_DISPLAY", "nothing-here")
    with pytest.raises(OSError):
        mod.Wire.connect()


# ----- the window list -----

def toplevel(comp, manager, handle, app_id=None, states=None, done=True):
    """A window's events; with manager, the handle is announced first."""
    if manager is not None:
        comp.event(manager, 0, u32(handle))
    if app_id is not None:
        comp.event(handle, 1, wl_string(app_id))
    if states is not None:
        comp.event(handle, 4, wl_array(states))
    if done:
        comp.event(handle, 5)


def test_the_games_window_coming_activated_and_closed(mod, comp):
    wire, registry = connected(mod, comp)
    changes = []
    tl = mod.Toplevels(registry, lambda: changes.append((tl.present("dosbox"),
                                                          tl.active("dosbox"))))
    comp.requests()
    m = tl.manager
    H1, H2 = 0xff000001, 0xff000002
    toplevel(comp, m, H1, "org.gnome.Console", [2])
    comp.event(H1, 0, wl_string("Console"))  # title: not used
    toplevel(comp, m, H2, "DOSBox", [])
    assert wire.read()
    assert changes == [(False, False), (True, False)]
    toplevel(comp, None, H1, states=[])
    toplevel(comp, None, H2, states=[3, 2])
    assert wire.read()
    assert changes[-2:] == [(True, False), (True, True)]
    # Closed: the handle is destroyed, the game gone
    comp.event(H2, 6)
    assert wire.read()
    assert changes[-1] == (False, False)
    assert comp.requests() == [(H2, 7, b"")]
    assert H2 not in wire.objects and set(tl.windows) == {H1}
    comp.event(m, 1)  # finished
    assert wire.read()


# ----- where the pads go -----

def test_pads_upright_lie_below_the_picture(mod):
    # The phone at scale 3: 360 x 800, the 4:3 picture is 360 x 270 in the middle
    p = mod.pads(360, 800)
    assert p["stick"] == (("bottom", "left"), {"bottom": 24}, (180, 241))
    assert p["buttons"] == (("bottom", "right"), {"bottom": 24}, (180, 241))
    # Their top edge is the picture's bottom edge
    assert 800 - 24 - 241 == (800 + 270) // 2


def test_pads_lying_fill_the_bars_beside_the_picture(mod):
    p = mod.pads(800, 360)
    assert p["stick"] == (("left",), {}, (160, 312))
    assert p["buttons"] == (("right",), {}, (160, 312))
    # A wider picture leaves less room: the smallest pad
    assert mod.pads(800, 360, 16 / 9)["stick"][2] == (mod.MIN_PAD, 312)


def test_pads_on_a_screen_without_room_lie_over_the_picture(mod):
    assert mod.pads(600, 600)["stick"][2] == (300, mod.MIN_PAD)
    assert mod.pads(601, 600)["buttons"][2] == (mod.MIN_PAD, 552)


def test_button_layout_diamond_and_row(mod):
    buttons = mod.button_layout(180, 241, ["ctrl", "space", "alt", "enter"], ["esc", "keyboard"])
    small, big = buttons[:2], buttons[2:]
    assert [b.name for b in small] == ["esc", "keyboard"] and not any(b.round for b in small)
    assert all(b.y == 8 and b.h == 40 and b.w == 72 for b in small)
    assert small[0].x + small[0].w + 8 == pytest.approx(small[1].x)
    assert [b.name for b in big] == ["ctrl", "space", "alt", "enter"] and all(b.round for b in big)
    centres = [(b.x + b.w / 2, b.y + b.h / 2) for b in big]
    ctrl, space, alt, enter = centres
    # Ctrl where the thumb rests (bottom), Space right, Alt left, Enter top
    assert ctrl[1] > space[1] and enter[1] < space[1] and space[0] > alt[0]
    assert ctrl[0] == pytest.approx(enter[0]) and space[1] == pytest.approx(alt[1])
    # Inside the pad, below the row, not overlapping
    for b in big:
        assert b.x >= 0 and b.x + b.w <= 180 and b.y > small[0].y + small[0].h and b.y + b.h <= 241
    for i, a in enumerate(centres):
        for c in centres[i + 1:]:
            assert math.dist(a, c) >= big[0].w


def test_lying_the_big_buttons_sit_low_a_buttons_height_above_the_edge(mod):
    # The phone lying: the bars beside the picture are 160 x 312, the screen 360 high
    names, small = ["ctrl", "space", "alt", "enter"], ["esc", "keyboard"]
    middle = mod.button_layout(160, 312, names, small)
    low = mod.button_layout(160, 312, names, small, low=True)
    # The small row stays along the top
    assert [(b.x, b.y) for b in low[:2]] == [(b.x, b.y) for b in middle[:2]]
    big = low[2:]
    for a, b in zip(middle[2:], big):
        assert b.y > a.y and b.x == a.x and b.w == a.w
    top = min(b.y for b in big)
    bottom = max(b.y + b.h for b in big)
    # Below the middle of the pad; under Ctrl, the lowest, one button's height stays free
    assert (top + bottom) / 2 > 312 / 2
    ctrl = big[0]
    assert ctrl.name == "ctrl" and ctrl.y + ctrl.h == bottom
    assert 312 - bottom == pytest.approx(ctrl.h)
    # A lone button: its own height free below it
    (one,) = mod.button_layout(160, 312, ["space"], [], low=True)
    assert 312 - (one.y + one.h) == pytest.approx(one.h)
    # A pad too short for that: at the edge, below the row
    flat = mod.button_layout(160, 120, names, small, low=True)
    row_bottom = flat[0].y + flat[0].h
    assert min(b.y for b in flat[2:]) > row_bottom
    assert max(b.y + b.h for b in flat[2:]) == pytest.approx(120 - 8)


def test_more_keys_in_rows_below_the_small_ones(mod):
    small, big = ["esc", "keyboard"], ["ctrl", "space", "alt", "enter"]
    weapons = [str(n) for n in range(1, 8)]
    assert mod.rows(small, weapons) == [small, weapons[:4], weapons[4:]]
    assert mod.rows([], weapons[:2]) == [weapons[:2]] and mod.rows(small) == [small]
    # Lying, the bar beside the picture 212 x 428: Esc and the keyboard, 1 to 4, 5 to 7
    out = mod.button_layout(212, 428, big, small, low=True, extra=weapons)
    names = [b.name for b in out]
    assert names == small + weapons + big
    keys = {b.name: b for b in out}
    assert keys["1"].y == keys["4"].y > keys["esc"].y + keys["esc"].h
    assert keys["5"].y == keys["7"].y > keys["1"].y + keys["1"].h
    assert not any(b.round for b in out[:9]) and all(b.round for b in out[9:])
    # The keys as wide as each other; the short row in the middle, inside the pad
    assert len({b.w for b in out[2:9]}) == 1
    assert keys["5"].x - 0 == pytest.approx(212 - (keys["7"].x + keys["7"].w))
    assert keys["1"].x >= 0 and keys["4"].x + keys["4"].w <= 212
    # The diamond below the rows, a button's height above the bottom as without them
    rows_end = keys["5"].y + keys["5"].h
    assert min(b.y for b in out[9:]) > rows_end
    ctrl = keys["ctrl"]
    assert 428 - (ctrl.y + ctrl.h) == pytest.approx(ctrl.h)
    assert ctrl.h == pytest.approx(mod.big_button_size(212, 428, big, small, weapons))
    # Upright the pad is short: the buttons shrink below the rows, still inside
    up = mod.button_layout(238, 326, big, small, extra=weapons)
    assert min(b.y for b in up[9:]) > max(b.y + b.h for b in up[:9])
    assert max(b.y + b.h for b in up[9:]) <= 326 - 8
    # Keys alone make the first row
    (one,) = mod.button_layout(212, 428, [], [], extra=["f1"])
    assert one.y == 8


def test_big_button_size_is_the_layouts(mod):
    for big in (["ctrl", "space", "alt", "enter"], ["ctrl", "space"], ["space"], []):
        for small in (["esc", "keyboard"], []):
            for w, h, low in ((160, 312, True), (180, 241, False), (160, 120, True)):
                buttons = [b for b in mod.button_layout(w, h, big, small, low) if b.round]
                size = mod.big_button_size(w, h, big, small)
                assert all(b.w == pytest.approx(size) for b in buttons)
                assert buttons or size == 0


def test_lying_the_stick_keeps_a_buttons_height_free_below_it(mod):
    s, board, felt = stick(mod)
    s.lift = 50
    s.resize(160, 312, low=True)
    r = 160 * 0.36
    assert s.rest == pytest.approx((80, 312 - r - 50)) and s.centre == s.rest
    # Less than the edge's 8 px never puts it lower than without
    s.lift = 3
    s.resize(160, 312, low=True)
    assert s.rest == pytest.approx((80, 312 - r - 8))


def test_low_centre(mod):
    # The lower third's middle when there is room, else as low as the control fits
    assert mod.low_centre(300, 20) == 250
    assert mod.low_centre(300, 80) == 220
    # Never above its own reach from the top
    assert mod.low_centre(100, 80) == 80


def test_button_layout_one_two_and_no_row(mod):
    (one,) = mod.button_layout(160, 312, ["space"], [])
    assert one.round and one.x + one.w / 2 == 80 and one.y + one.h / 2 == pytest.approx(156)
    a, b = mod.button_layout(160, 312, ["ctrl", "alt"], [])
    assert a.x < b.x and a.y > b.y
    assert mod.button_layout(160, 312, [], []) == []
    # Many small buttons share the width
    row = mod.button_layout(100, 200, [], ["f1", "f2", "f3", "f4"])
    assert all(b.w == pytest.approx((100 - 40) / 4) for b in row)
    # A flat pad leaves no room for big buttons, but no negative size
    (flat,) = mod.button_layout(100, 10, ["ctrl"], [])
    assert flat.w == 0


def test_five_big_buttons_as_on_an_arcade_panel(mod):
    # Mortal Kombat: high punch, block, high kick above; low punch, low kick below
    names = ["kp7", "kp5", "kp9", "kp1", "kp3"]
    small = ["esc", "enter", "f10", "keyboard"]
    for w, h, low, extra in ((211, 429, True, ["f1", "f2"]), (238, 326, False, ["f1", "f2"]),
                             (160, 312, True, []), (160, 120, True, [])):
        out = mod.button_layout(w, h, names, small, low, extra)
        big = [b for b in out if b.round]
        assert [b.name for b in big] == names
        hp, block, hk, lp, lk = (centre(b) for b in big)
        # Three in a row above, two below between them
        assert hp[1] == pytest.approx(block[1]) and hk[1] == pytest.approx(block[1])
        assert lp[1] == pytest.approx(lk[1]) and lp[1] > hp[1]
        assert hp[0] < lp[0] < block[0] < lk[0] < hk[0]
        assert block[0] == pytest.approx(w / 2)
        size = mod.big_button_size(w, h, names, small, extra)
        assert all(b.w == pytest.approx(size) == pytest.approx(b.h) for b in big)
        # Inside the pad, below the rows, not overlapping
        rows_end = max(b.y + b.h for b in out if not b.round)
        for b in big:
            assert b.x >= 0 and b.x + b.w <= w and b.y > rows_end and b.y + b.h <= h
        points = [hp, block, hk, lp, lk]
        for i, a in enumerate(points):
            for c in points[i + 1:]:
                assert math.dist(a, c) >= size
    # Lying, a button's height free below the lower two, as below Ctrl
    out = mod.button_layout(211, 429, names, small, low=True, extra=["f1", "f2"])
    lp = next(b for b in out if b.name == "kp1")
    assert 429 - (lp.y + lp.h) == pytest.approx(lp.h)
    # As large as a diamond's buttons lying: the bar's width decides, not the rows
    diamond = mod.big_button_size(211, 429, ["ctrl", "space", "alt", "enter"], small)
    assert mod.big_button_size(211, 429, names, small, ["f1", "f2"]) > 0.75 * diamond


def test_a_touch_between_two_buttons_takes_the_nearer(mod):
    board = Board()
    b = mod.Buttons(mod.Keys(board), ["kp7", "kp5", "kp9", "kp1", "kp3"], [], {})
    b.resize(211, 429, low=True)
    hp, block = b.buttons[0], b.buttons[1]
    (x1, y), (x2, _) = centre(hp), centre(block)
    # Both are hit just right of the middle between them; the block is nearer
    x = (x1 + x2) / 2 + 0.5
    assert hp.hit(x, y) and block.hit(x, y)
    assert b.down("t", x, y) and block.pressed and not hp.pressed
    assert board.calls[0] == ("key", 76, True)


def test_button_hit_and_label(mod):
    b = mod.Button("ctrl", 10, 10, 40, 40, True)
    assert b.hit(30, 30) and b.hit(30, 30 + 24) and not b.hit(30, 30 + 25)
    assert b.label == "Ctrl"
    r = mod.Button("keyboard", 10, 10, 60, 30, False)
    assert r.hit(6, 6) and r.hit(74, 44) and not r.hit(5, 30) and r.label == ""


@pytest.mark.parametrize("dx, dy, held", [
    (10, 0, {"right"}), (-10, 0, {"left"}), (0, -10, {"up"}), (0, 10, {"down"}),
    (10, -10, {"up", "right"}), (-10, 10, {"down", "left"}), (10, 10, {"down", "right"}),
    # 20 degrees from the right: still right only; 25 degrees: the diagonal
    (math.cos(math.radians(20)) * 10, -math.sin(math.radians(20)) * 10, {"right"}),
    (math.cos(math.radians(25)) * 10, -math.sin(math.radians(25)) * 10, {"right", "up"}),
    (2, 2, set()),
])
def test_stick_directions(mod, dx, dy, held):
    assert mod.stick_directions(dx, dy, 10) == held


def test_stick_without_a_size_holds_nothing(mod):
    assert mod.stick_directions(10, 0, 0) == set()


# ----- the controls -----

def stick(mod, names=("up", "down", "left", "right")):
    board = Board()
    felt = []
    s = mod.Stick(mod.Keys(board), names, lambda: felt.append(1))
    s.resize(180, 241)
    return s, board, felt


def test_stick_centre_goes_where_the_finger_lands(mod):
    s, board, felt = stick(mod)
    r = s.radius
    assert r == pytest.approx(180 * 0.36) and s.centre == (90, 120.5)
    assert s.down("t1", 100, 150)
    assert s.centre == (100, 150) and board.calls == [] and felt == []
    assert s.move("t1", 130, 150)
    assert board.calls == [("key", 106, True)] and s.held == {"right"} and felt == [1]
    # Pushed beyond the rim: the knob stays on it
    s.move("t1", 100 + 3 * r, 150)
    assert s.knob == pytest.approx((100 + r, 150))
    # Round to the diagonal and on to up: right goes, up comes; no second buzz
    s.move("t1", 130, 120)
    s.move("t1", 100, 120)
    assert board.calls[1:] == [("key", 103, True), ("key", 106, False)] and felt == [1]
    # A second finger and other fingers' moves do nothing
    assert not s.down("t2", 10, 10) and not s.move("t2", 0, 0) and not s.up("t2")
    assert s.up("t1")
    assert board.calls[-1] == ("key", 103, False) and s.touch is None
    assert s.centre == s.knob == (90, 120.5)
    assert not s.up("t1")


def test_stick_centre_stays_inside_the_pad(mod):
    s, board, felt = stick(mod)
    s.down("t", 0, 241)
    assert s.centre == pytest.approx((s.radius, 241 - s.radius))
    s.release_all()
    assert s.touch is None
    # Resized while held: the centre stays where the finger is
    s.down("t", 50, 100)
    held = s.centre
    s.resize(160, 312)
    assert s.centre == held and s.radius == pytest.approx(160 * 0.36)


def test_lying_the_stick_rests_in_the_lower_third(mod):
    s, board, felt = stick(mod)
    s.resize(160, 312, low=True)
    r = 160 * 0.36
    # As low as it fits: the lower third's middle would leave it over the edge
    assert s.rest == pytest.approx((80, 312 - r - 8)) and s.centre == s.rest
    assert 24 + s.rest[1] >= 360 * 2 / 3
    # Back to rest there after a touch
    s.down("t", 80, 100)
    assert s.centre != s.rest
    s.up("t")
    assert s.centre == s.rest
    # The cross is bigger, still low; the style button keeps it low
    t = s.toggle
    s.down("u", t.x + t.w / 2, t.y + t.h / 2)
    s.up("u")
    assert s.style == "dpad" and s.low
    assert s.centre == pytest.approx((80, 312 - 160 * 0.42 - 8))
    # Upright again: the middle
    s.resize(180, 241, low=False)
    assert s.centre == (90, 120.5)


def test_stick_with_wasd(mod):
    s, board, felt = stick(mod, mod.STICKS["wasd"])
    s.down(1, 90, 120)
    s.move(1, 60, 150)
    assert sorted(board.calls) == [("key", 30, True), ("key", 31, True)]


def test_the_style_is_kept_across_games(mod, state_home, capsys):
    assert mod.style_path() == str(state_home / "plinius-touch-controls" / "style")
    assert mod.load_style() == "stick"
    mod.save_style("dpad")
    assert (state_home / "plinius-touch-controls" / "style").read_text() == "dpad\n"
    assert mod.load_style() == "dpad"
    (state_home / "plinius-touch-controls" / "style").write_text("wheel\n")
    assert mod.load_style() == "stick"


def test_the_style_without_a_state_home(mod, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert mod.style_path() == str(tmp_path / ".local/state/plinius-touch-controls/style")
    # A file where the directory should be: not kept, said so, no crash
    (tmp_path / ".local").write_text("")
    mod.save_style("dpad")
    assert "style not kept" in capsys.readouterr().err
    assert mod.load_style() == "stick"


def test_the_corner_button_switches_the_style(mod):
    board = Board()
    felt, styled = [], []
    s = mod.Stick(mod.Keys(board), mod.STICKS["arrows"], lambda: felt.append(1), "wheel",
                  styled.append)
    assert s.style == "stick"
    s.resize(180, 241)
    t = s.toggle
    assert (t.x, t.y, t.w) == (8, 8, 36)
    assert s.down("t1", t.x + t.w / 2, t.y + t.h / 2)
    assert s.style == "dpad" and styled == ["dpad"] and felt == [1]
    assert s.toggle.pressed and s.radius == pytest.approx(180 * 0.42)
    # The finger on the button moves no stick; no other finger while it is down
    assert s.move("t1", 90, 120) and board.calls == []
    assert not s.down("t2", 90, 120) and not s.move("t2", 90, 60)
    assert s.up("t1") and not s.toggle.pressed and s.toggle_touch is None
    # Back again
    s.down("t3", 20, 20)
    s.up("t3")
    assert s.style == "stick" and styled == ["dpad", "stick"]
    # While the stick is held the button does nothing
    s.down("t4", 90, 120)
    assert not s.down("t5", 20, 20) and s.style == "stick"
    s.release_all()
    # Let go of everything also lets go of the button
    s.down("t6", 20, 20)
    s.release_all()
    assert s.toggle_touch is None and not s.toggle.pressed and s.style == "dpad"


def test_the_cross_stays_and_counts_from_its_middle(mod):
    board = Board()
    s = mod.Stick(mod.Keys(board), mod.STICKS["arrows"], style="dpad")
    s.resize(180, 241)
    r = s.radius
    assert s.centre == (90, 120.5)
    # Down on the right arm: right at once, the cross does not move
    assert s.down("t", 90 + r * 0.6, 120.5)
    assert s.centre == (90, 120.5) and s.held == {"right"}
    assert board.calls == [("key", 106, True)]
    # A slight roll counts (smaller middle than the stick's)
    s.move("t", 90, 120.5 - r * 0.2)
    assert s.held == {"up"}
    # Far outside the cross still counts, diagonals hold two
    s.move("t", 0, 241)
    assert s.held == {"down", "left"}
    s.move("t", 90, 121)
    assert s.held == set()
    assert s.up("t") and s.centre == (90, 120.5)


def buttons(mod, big=("ctrl", "space"), small=("esc", "keyboard")):
    board = Board()
    felt, actions = [], []
    b = mod.Buttons(mod.Keys(board), big, small, {"keyboard": lambda: actions.append("osk")},
                    lambda: felt.append(1))
    b.resize(180, 241)
    return b, board, felt, actions


def centre(btn):
    return btn.x + btn.w / 2, btn.y + btn.h / 2


def test_a_finger_holds_its_button_until_it_lifts(mod):
    b, board, felt, actions = buttons(mod)
    esc, kbd, ctrl, space = b.buttons
    assert b.down("t1", *centre(ctrl))
    assert ctrl.pressed and board.calls == [("key", 29, True), ("mods", 4)] and felt == [1]
    # Sliding off keeps it
    assert b.move("t1", 0, 0) and not b.move("t9", 0, 0)
    assert b.down("t2", *centre(space))
    assert b.up("t1") and not ctrl.pressed and space.pressed
    assert board.calls[2:] == [("key", 57, True), ("key", 29, False), ("mods", 0)]
    assert not b.up("t1")
    # Nothing there: not taken
    assert not b.down("t3", 179, 240)
    b.release_all()
    assert board.calls[-1] == ("key", 57, False) and not space.pressed and b.touches == {}


def test_two_fingers_on_one_button(mod):
    b, board, felt, actions = buttons(mod)
    ctrl = b.buttons[2]
    b.down("a", *centre(ctrl))
    b.down("b", *centre(ctrl))
    b.up("a")
    assert ctrl.pressed and ("key", 29, False) not in board.calls
    b.up("b")
    assert not ctrl.pressed and board.calls[-2:] == [("key", 29, False), ("mods", 0)]


def test_the_keyboard_button_is_an_action(mod):
    b, board, felt, actions = buttons(mod)
    kbd = b.buttons[1]
    assert b.down("t", *centre(kbd)) and actions == ["osk"] and board.calls == []
    assert b.up("t") and board.calls == [] and not kbd.pressed


def test_resizing_lets_go(mod):
    b, board, felt, actions = buttons(mod)
    b.down("t", *centre(b.buttons[0]))
    b.resize(160, 312)
    assert board.calls == [("key", 1, True), ("key", 1, False)] and b.touches == {}


# ----- drawing -----

def surface(w=180, h=241):
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    return s, cairo.Context(s)


def alpha(s, x, y):
    s.flush()
    return s.get_data()[int(y) * s.get_stride() + int(x) * 4 + 3]


def test_draw_stick_idle_and_held(mod):
    s, board, felt = stick(mod)
    img, cr = surface()
    mod.draw_stick(cr, s)
    idle = alpha(img, *s.centre)
    assert idle > 0 and alpha(img, 2, 2) == 0
    s.down(1, 90, 120)
    s.move(1, 90, 60)
    img2, cr2 = surface()
    mod.draw_stick(cr2, s)
    assert alpha(img2, *s.knob) > idle
    # The lit arrow (up) is brighter than the one opposite
    cx, cy = s.centre
    up = alpha(img2, cx, cy - s.radius * 0.8)
    down = alpha(img2, cx, cy + s.radius * 0.8)
    assert up > down


def test_draw_the_cross_and_the_corner_button(mod):
    board = Board()
    s = mod.Stick(mod.Keys(board), mod.STICKS["arrows"], style="dpad")
    s.resize(180, 241)
    cx, cy = s.centre
    r = s.radius
    img, cr = surface()
    mod.draw_stick(cr, s)
    arm = alpha(img, cx + r * 0.45, cy)
    # Inside the arms, nothing between them (the corner of the plus)
    assert arm > 0 and alpha(img, cx + r * 0.7, cy + r * 0.7) == 0
    # The corner button shows the other style: a circle with a dot
    t = s.toggle
    assert alpha(img, t.x + t.w / 2, t.y + t.h / 2) > alpha(img, t.x + t.w * 0.3, t.y + t.h / 2)
    s.down(1, cx + r * 0.6, cy)
    img2, cr2 = surface()
    mod.draw_stick(cr2, s)
    assert alpha(img2, cx + r * 0.45, cy) > arm
    assert alpha(img2, cx - r * 0.45, cy) == arm
    # Each arm lights where it is
    for x, y, d in ((cx, cy - r * 0.6, "up"), (cx, cy + r * 0.6, "down"),
                    (cx - r * 0.6, cy, "left")):
        s.release_all()
        s.down(1, x, y)
        img3, cr3 = surface()
        mod.draw_stick(cr3, s)
        assert s.held == {d}
        assert alpha(img3, (x + cx) / 2 + (x - cx) * 0.1, (y + cy) / 2 + (y - cy) * 0.1) > arm
    # The stick style shows a cross in its corner button
    s2, board2, felt2 = stick(mod)
    img4, cr4 = surface()
    mod.draw_stick(cr4, s2)
    t = s2.toggle
    assert alpha(img4, t.x + t.w / 2, t.y + t.h * 0.3) > 0


def test_draw_buttons(mod):
    b, board, felt, actions = buttons(mod, big=("ctrl", "space", "alt", "enter"),
                                      small=("esc", "keyboard", "pgup"))
    img, cr = surface()
    mod.draw_buttons(cr, b.buttons)
    ctrl = b.buttons[3]
    edge = alpha(img, ctrl.x + 3, ctrl.y + ctrl.h / 2)
    assert edge > 0 and alpha(img, 1, 239) == 0
    # Nothing between the buttons: no stroke from one label to the next circle
    enter = b.buttons[6]
    middle = (ctrl.x + ctrl.w / 2, (ctrl.y + ctrl.h / 2 + enter.y + enter.h / 2) / 2)
    assert alpha(img, *middle) == 0
    b.down("t", ctrl.x + 3, ctrl.y + ctrl.h / 2)
    img2, cr2 = surface()
    mod.draw_buttons(cr2, b.buttons)
    assert alpha(img2, ctrl.x + 3, ctrl.y + ctrl.h / 2) > edge
    # The keyboard icon and the labels draw something inside their buttons
    kbd = b.buttons[1]
    img.flush()
    data = img.get_data()
    inside = [data[int(y) * img.get_stride() + int(x) * 4 + 3]
              for y in range(int(kbd.y) + 4, int(kbd.y + kbd.h) - 4)
              for x in range(int(kbd.x) + 8, int(kbd.x + kbd.w) - 8)]
    assert max(inside) > 150


# ----- the pads -----

class Shell:
    """GtkLayerShell, recording."""

    class Layer:
        OVERLAY = "overlay"

    class Edge:
        TOP, BOTTOM, LEFT, RIGHT = "top", "bottom", "left", "right"

    class KeyboardMode:
        NONE = "none"

    def __init__(self, supported=True):
        self.calls = []
        self.supported = supported

    def is_supported(self):
        return self.supported

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda window, *args: self.calls.append((name,) + args)


class Area:
    def __init__(self):
        self.handlers = {}
        self.events = None
        self.size = None
        self.draws = 0

    def connect(self, signal, handler):
        self.handlers[signal] = handler

    def add_events(self, mask):
        self.events = mask

    def set_size_request(self, w, h):
        self.size = (w, h)

    def queue_draw(self):
        self.draws += 1


class EventType:
    TOUCH_BEGIN, TOUCH_UPDATE, TOUCH_END, TOUCH_CANCEL = "begin", "update", "end", "cancel"
    BUTTON_PRESS, BUTTON_RELEASE, DOUBLE_BUTTON_PRESS = "press", "release", "double"


def ev(kind, x=0, y=0, seq="s", button=1):
    return types.SimpleNamespace(type=kind, x=x, y=y, sequence=seq, button=button)


@pytest.fixture
def gtk(mod, monkeypatch):
    shell = Shell()
    monkeypatch.setattr(mod, "GtkLayerShell", shell)
    monkeypatch.setattr(mod.Gtk, "DrawingArea", Area)
    monkeypatch.setattr(mod.Gdk, "EventType", EventType)
    state = types.SimpleNamespace(shell=shell, visuals=[], added=[], resized=[], shown=[])
    screen = types.SimpleNamespace(get_rgba_visual=lambda: "rgba")
    monkeypatch.setattr(mod.Pad, "get_screen", lambda self: screen, raising=False)
    monkeypatch.setattr(mod.Pad, "set_visual", lambda self, v: state.visuals.append(v),
                        raising=False)
    monkeypatch.setattr(mod.Pad, "add", lambda self, c: state.added.append(c), raising=False)
    monkeypatch.setattr(mod.Pad, "resize", lambda self, w, h: state.resized.append((w, h)),
                        raising=False)
    monkeypatch.setattr(mod.Pad, "show_all", lambda self: state.shown.append(True), raising=False)
    monkeypatch.setattr(mod.Pad, "hide", lambda self: state.shown.append(False), raising=False)
    return state


def test_pad_is_a_transparent_overlay_without_keyboard_focus(mod, gtk):
    s, board, felt = stick(mod)
    pad = mod.Pad(s, mod.draw_stick)
    assert gtk.shell.calls == [("init_for_window",), ("set_namespace", "plinius-touch-controls"),
                               ("set_layer", "overlay"), ("set_exclusive_zone", -1),
                               ("set_keyboard_mode", "none")]
    assert gtk.visuals == ["rgba"] and gtk.added == [pad.area]
    assert set(pad.area.handlers) == {"draw", "touch-event", "button-press-event",
                                      "button-release-event", "motion-notify-event"}
    gtk.shell.calls.clear()
    pad.place("panel", ("bottom", "left"), {"bottom": 24}, (180, 241))
    assert gtk.shell.calls == [("set_monitor", "panel"),
                               ("set_anchor", "top", False), ("set_margin", "top", 0),
                               ("set_anchor", "bottom", True), ("set_margin", "bottom", 24),
                               ("set_anchor", "left", True), ("set_margin", "left", 0),
                               ("set_anchor", "right", False), ("set_margin", "right", 0)]
    assert pad.area.size == (180, 241) and gtk.resized == [(180, 241)] and s.w == 180


def test_pad_without_an_alpha_visual(mod, gtk, monkeypatch):
    monkeypatch.setattr(mod.Pad, "get_screen", lambda self: types.SimpleNamespace(
        get_rgba_visual=lambda: None), raising=False)
    mod.Pad(stick(mod)[0], mod.draw_stick)
    assert gtk.visuals == []


def test_pad_passes_touches_and_the_mouse_on(mod, gtk):
    s, board, felt = stick(mod)
    pad = mod.Pad(s, mod.draw_stick)
    pad.place("panel", ("left",), {}, (180, 241), low=True)
    assert s.low
    pad.place("panel", ("left",), {}, (180, 241))
    assert not s.low
    draws = pad.area.draws
    assert pad.on_touch(None, ev("begin", 90, 120, "a")) is True
    pad.on_touch(None, ev("update", 130, 120, "a"))
    assert board.calls == [("key", 106, True)]
    pad.on_touch(None, ev("update", 0, 0, "other"))
    pad.on_touch(None, ev("cancel", seq="a"))
    pad.on_touch(None, ev("something else"))
    assert board.calls[-1] == ("key", 106, False) and pad.area.draws == draws + 3
    # The mouse: left button only
    pad.on_button(None, ev("press", 90, 120, button=3))
    pad.on_button(None, ev("press", 90, 120))
    pad.on_motion(None, ev(None, 90, 80))
    pad.on_motion(None, ev(None, 90, 70))
    pad.on_button(None, ev("double", 90, 120))
    assert pad.on_button(None, ev("release")) is True
    assert board.calls[-2:] == [("key", 103, True), ("key", 103, False)]
    # Moves without a held button do nothing
    draws = pad.area.draws
    assert pad.on_motion(None, ev(None, 1, 1)) is True and pad.area.draws == draws
    pad.on_touch(None, ev("begin", 90, 120, "z"))
    pad.release_all()
    assert s.touch is None


def test_pad_draws_on_a_cleared_surface(mod, gtk):
    b, board, felt, actions = buttons(mod)
    pad = mod.Pad(b, lambda cr, c: mod.draw_buttons(cr, c.buttons))
    img, cr = surface()
    cr.set_source_rgba(1, 0, 0, 1)
    cr.paint()
    assert pad.on_draw(None, cr) is True
    assert alpha(img, 179, 240) == 0 and alpha(img, *centre(b.buttons[2])) > 0


# ----- the overlay -----

class FakePad:
    def __init__(self, control, draw):
        self.control = control
        self.draw = draw
        self.placed = []
        self.visible = None
        self.released = 0

    def place(self, monitor, anchors, margins, size, low=False):
        self.placed.append((anchors, margins, size, low))
        self.control.resize(*size, low)

    def show_all(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def release_all(self):
        self.released += 1
        self.control.release_all()


class Monitor:
    def __init__(self, w=360, h=800, mm=(64, 143)):
        self.w, self.h = w, h
        self.mm = mm
        self.handlers = {}

    def get_width_mm(self):
        return self.mm[0]

    def get_height_mm(self):
        return self.mm[1]

    def get_geometry(self):
        return types.SimpleNamespace(width=self.w, height=self.h)

    def connect(self, signal, handler):
        self.handlers[signal] = handler

    def turn(self):
        self.w, self.h = self.h, self.w
        self.handlers["notify::geometry"](self, None)


class Display:
    def __init__(self, monitors, primary=None):
        self.monitors = monitors
        self.primary = primary

    def get_n_monitors(self):
        return len(self.monitors)

    def get_monitor(self, i):
        return self.monitors[i] if i < len(self.monitors) else None

    def get_primary_monitor(self):
        return self.primary


class Session:
    """The session bus: calls recorded, answered by finish."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def call(self, name, path, iface, method, params, reply, flags, timeout, cancel, done, what):
        self.calls.append((name, method, params.unpack()))
        done(self, "result", what)

    def call_finish(self, result):
        if self.fail:
            raise fakes.gerror(self.fail)


@pytest.fixture
def overlay(mod, monkeypatch):
    monkeypatch.setattr(mod, "Pad", FakePad)

    def make(session=None, **kw):
        board = Board()
        o = mod.Overlay(mod.Keys(board), "dosbox", kw.get("stick", "arrows"),
                        kw.get("big", ["ctrl", "space"]), kw.get("small", ["esc", "keyboard"]),
                        session=session, extra=kw.get("extra", ()))
        o.board = board
        return o
    return make


def test_overlay_takes_and_keeps_the_style(mod, overlay, state_home):
    assert overlay().stick.style == "stick"
    mod.save_style("dpad")
    o = overlay()
    assert o.stick.style == "dpad"
    o.place = lambda: None
    o.stick.resize(180, 241)
    t = o.stick.toggle
    o.stick.down("t", t.x + 1 + t.w / 2, t.y + t.h / 2)
    assert mod.load_style() == "stick"


def test_overlay_places_the_pads_and_follows_a_turn(mod, overlay):
    o = overlay()
    phone = Monitor()
    assert o.attach(Display([Monitor(0, 0, (0, 0)), phone]))
    assert o.monitor is phone
    # Upright below the picture, the controls in the middle of the pads
    assert o.pads["stick"].placed == [(("bottom", "left"), {"bottom": 24}, (180, 241), False)]
    assert o.stick.rest == (90, 120.5)
    # Lying beside it, the controls low
    phone.turn()
    assert o.pads["buttons"].placed[-1] == (("right",), {}, (160, 312), True)
    assert o.stick.radius == pytest.approx(160 * 0.36) and o.stick.low
    # The stick rests a big button's height (Ctrl's) above the bottom of its pad
    ctrl = o.buttons.buttons[2]
    assert ctrl.name == "ctrl" and o.stick.lift == pytest.approx(ctrl.h)
    assert 312 - o.stick.rest[1] - o.stick.radius == pytest.approx(ctrl.h)
    # Upright again: no room kept, the stick in the middle
    phone.turn()
    assert o.stick.lift == 0 and o.stick.rest == (90, 120.5)
    # More keys in rows: the pair still keeps a button's height free, the stick as high
    o = overlay(extra=["1", "2", "3", "4", "5"])
    phone.turn()
    assert o.attach(Display([phone]))
    assert [b.name for b in o.buttons.buttons][2:7] == ["1", "2", "3", "4", "5"]
    ctrl = o.buttons.buttons[7]
    assert ctrl.name == "ctrl" and 312 - (ctrl.y + ctrl.h) == pytest.approx(ctrl.h)
    assert o.stick.lift == pytest.approx(ctrl.h)
    # This bar is too short for three rows, the diamond and a button's height below: the
    # diamond goes down to the edge, below the rows, and the stick with it
    o = overlay(extra=["1", "2", "3", "4", "5"], big=["ctrl", "space", "alt", "enter"])
    assert o.attach(Display([phone]))
    ctrl = o.buttons.buttons[7]
    rows_end = max(b.y + b.h for b in o.buttons.buttons[:7])
    assert min(b.y for b in o.buttons.buttons[7:]) > rows_end
    assert ctrl.name == "ctrl" and 312 - (ctrl.y + ctrl.h) == pytest.approx(8)
    assert o.stick.lift == pytest.approx(8)
    assert 312 - o.stick.rest[1] - o.stick.radius == pytest.approx(8)
    # Without big buttons nothing to line up with
    o = overlay(big=[])
    assert o.attach(Display([phone])) and o.stick.lift == 0


def test_overlay_falls_back_to_the_primary_monitor_or_none(mod, overlay):
    o = overlay()
    screen = Monitor(1920, 1080, (0, 0))
    assert o.attach(Display([screen], primary=screen)) and o.monitor is screen
    assert not overlay().attach(Display([]))


def test_overlay_shows_while_the_game_is_active_and_the_osk_folded(mod, overlay):
    o = overlay()
    o.attach(Display([Monitor()]))
    o.update()
    assert all(p.visible for p in o.pads.values())
    # A key held while the pads hide goes up
    o.buttons.down("t", *centre(o.buttons.buttons[2]))
    o.osk_visible = True
    o.update()
    assert not any(p.visible for p in o.pads.values())
    assert o.board.calls[-2:] == [("key", 29, False), ("mods", 0)]
    released = o.pads["stick"].released
    o.update()  # nothing changed: nothing done
    assert o.pads["stick"].released == released


def test_overlay_follows_the_window_list(mod, overlay):
    o = overlay()
    o.attach(Display([Monitor()]))
    tl = types.SimpleNamespace(present=lambda a: state["present"], active=lambda a: state["active"])
    state = {"present": False, "active": False}
    # Before the game's window comes: wait for it
    assert o.windows_changed(tl) is True and not o.shown
    state.update(present=True, active=True)
    assert o.windows_changed(tl) is True and o.shown
    state.update(active=False)
    assert o.windows_changed(tl) is True and not o.shown
    # Gone after it was there: the end
    state.update(present=False)
    assert o.windows_changed(tl) is False


class OskProxy:
    def __init__(self, visible):
        self.visible = visible
        self.handlers = {}

    def get_cached_property(self, name):
        return None if self.visible is None else mod_glib().Variant("b", self.visible)

    def connect(self, signal, handler):
        self.handlers[signal] = handler


def mod_glib():
    from gi.repository import GLib
    return GLib


def test_overlay_watches_the_osk(mod, overlay, monkeypatch, capsys):
    made = []
    proxy = OskProxy(False)

    def new(bus, flags, info, name, path, iface, cancel, done):
        made.append((bus, name, path, iface))
        done(None, "result")
    gio = fakes.Proxy(mod.Gio, DBusProxy=types.SimpleNamespace(
        new=new, new_finish=lambda result: proxy))
    monkeypatch.setattr(mod, "Gio", gio)
    o = overlay(Session())
    o.attach(Display([Monitor()]))
    o.watch_osk()
    assert made == [(o.session, "sm.puri.OSK0", "/sm/puri/OSK0", "sm.puri.OSK0")]
    assert o.shown and not o.osk_visible
    proxy.visible = True
    proxy.handlers["g-properties-changed"](proxy, None, None)
    assert o.osk_visible and not o.shown
    proxy.visible = None
    proxy.handlers["g-properties-changed"](proxy, None, None)
    assert not o.osk_visible and o.shown

    def broken(result):
        raise fakes.gerror("no OSK")
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(mod.Gio, DBusProxy=types.SimpleNamespace(
        new=new, new_finish=broken)))
    overlay(Session()).watch_osk()
    assert "plinius-touch-controls: on-screen keyboard: no OSK" in capsys.readouterr().err
    # Without a session bus nothing is watched or called
    o = overlay(None)
    o.watch_osk()
    o.show_keyboard()
    o.feedback()


def test_overlay_unfolds_the_osk_and_buzzes(mod, overlay, capsys):
    session = Session()
    o = overlay(session)
    o.attach(Display([Monitor()]))
    kbd = o.buttons.buttons[1]
    o.buttons.down("t", *centre(kbd))
    assert session.calls == [
        ("org.sigxcpu.Feedback", "TriggerFeedback",
         ("org.plinius.TouchControls", "button-pressed", {}, -1)),
        ("sm.puri.OSK0", "SetVisible", (True,))]
    failing = overlay(Session(fail="not there"))
    failing.show_keyboard()
    assert "plinius-touch-controls: SetVisible: not there" in capsys.readouterr().err


# ----- the program -----

def test_key_lists_and_aspect(mod, capsys):
    assert mod.key_list("Ctrl, alt,,ctrl,nope,space,enter,esc", 4, mod.KEYS) == [
        "ctrl", "alt", "space", "enter"]
    assert "no such key: nope" in capsys.readouterr().err
    assert mod.aspect_ratio("16:9") == pytest.approx(16 / 9)
    assert mod.aspect_ratio("1.25") == 1.25
    for bad in ("x", "4:0", "10:1", "1:9"):
        with pytest.raises(mod.argparse.ArgumentTypeError):
            mod.aspect_ratio(bad)


def test_arguments(mod):
    a = mod.parse_args([])
    assert (a.app_id, a.stick, a.buttons, a.small) == (
        "dosbox", "arrows", ["ctrl", "space", "alt", "enter"], ["esc", "keyboard"])
    assert a.aspect == pytest.approx(4 / 3) and a.keys == []
    a = mod.parse_args(["--keys", "1,2,3,keyboard,4,5,6,7,8,9,0,a,b,c"])
    assert a.keys == ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "a", "b"]
    a = mod.parse_args(["--buttons", "kp7,kp5,kp9,kp1,kp3,ctrl"])
    assert a.buttons == ["kp7", "kp5", "kp9", "kp1", "kp3"]
    a = mod.parse_args(["--stick", "wasd", "--buttons", "", "--small", "keyboard,f1,tab,esc,y",
                        "--app-id", "game"])
    assert (a.app_id, a.stick, a.buttons, a.small) == ("game", "wasd", [], [
        "keyboard", "f1", "tab", "esc"])
    with pytest.raises(SystemExit):
        mod.parse_args(["--stick", "mouse"])


class Loop:
    """GLib and Gtk for main(): the watch and signal handlers kept, Gtk.main runs a script."""

    def __init__(self, mod, script):
        self.mod = mod
        self.script = script
        self.watches = []
        self.signals = []
        self.quits = 0
        self.timers = {}
        self.glib = fakes.Proxy(mod.GLib, io_add_watch=self.io_add_watch,
                                unix_signal_add=self.unix_signal_add,
                                timeout_add_seconds=self.timeout_add_seconds,
                                source_remove=self.timers.pop)
        gtk = fakes.namespace("Gtk")
        gtk.init = lambda argv: None
        gtk.main = lambda: script(self)
        gtk.main_quit = self.main_quit
        self.gtk = gtk

    def io_add_watch(self, fd, priority, condition, func):
        self.watches.append(func)
        return 1

    def timeout_add_seconds(self, seconds, func):
        tid = 10 + len(self.timers)
        self.timers[tid] = (seconds, func)
        return tid

    def fire(self):
        """Runs the pending timers as if their time had come."""
        for tid in list(self.timers):
            seconds, func = self.timers.pop(tid)
            assert func() is False

    def unix_signal_add(self, priority, sig, func):
        self.signals.append((sig, func))
        return 2

    def main_quit(self):
        self.quits += 1


def run_main(mod, monkeypatch, comp, script, globals_=Compositor.GLOBALS, session="bus",
             monitors=None, argv=()):
    comp.announce(globals_)
    loop = Loop(mod, script)
    monkeypatch.setattr(mod, "GLib", loop.glib)
    monkeypatch.setattr(mod, "Gtk", loop.gtk)
    monkeypatch.setattr(mod, "GtkLayerShell", Shell())
    monkeypatch.setattr(mod, "Pad", FakePad)
    monkeypatch.setattr(mod.Wire, "connect", classmethod(lambda cls, env=None: cls(comp.client)))

    def bus_get_sync(kind, cancel):
        if session is None:
            raise fakes.gerror("no bus")
        return Session()
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(mod.Gio, bus_get_sync=bus_get_sync,
                                                DBusProxy=types.SimpleNamespace(
                                                    new=lambda *a: None)))
    display = Display(monitors if monitors is not None else [Monitor()])
    monkeypatch.setattr(mod, "Gdk", fakes.Proxy(mod.Gdk, Display=types.SimpleNamespace(
        get_default=lambda: display)))
    code = mod.main(list(argv))
    return code, loop


def test_main_shows_the_controls_while_dosbox_is_active_and_ends_with_it(mod, monkeypatch, comp,
                                                                          capsys):
    seen = {}

    def script(loop):
        H = 0xff000010
        manager = 7  # registry 2, callback 3, seat 4, vk manager 5, vk 6, toplevel manager 7
        toplevel(comp, manager, H, "dosbox", [2])
        assert loop.watches[0](None, None) is True
        seen["requests"] = comp.requests()
        # A key held when DOSBox closes goes up; the controls end only when it stays gone
        comp.event(H, 6)
        assert loop.watches[0](None, None) is True
        assert loop.quits == 0 and [s for s, f in loop.timers.values()] == [mod.GONE_GRACE]
        loop.fire()
        seen["quits"] = loop.quits
    code, loop = run_main(mod, monkeypatch, comp, script, argv=["--stick", "wasd"])
    assert code == 0 and seen["quits"] == 1
    assert "plinius-touch-controls: dosbox is gone" in capsys.readouterr().err
    reqs = seen["requests"]
    assert (2, 0, u32(3) + wl_string("zwlr_foreign_toplevel_manager_v1") + u32(3, 7)) in reqs
    assert {s for s, f in loop.signals} == {mod.signal.SIGTERM, mod.signal.SIGINT}


def test_main_stays_when_dosbox_opens_a_new_window_while_starting(mod, monkeypatch, comp,
                                                                  capsys):
    seen = {}

    def script(loop):
        manager = 7
        toplevel(comp, manager, 0xff000010, "dosbox", [2])
        assert loop.watches[0](None, None) is True
        comp.event(0xff000010, 6)
        assert loop.watches[0](None, None) is True
        assert len(loop.timers) == 1
        # The second window comes before the time is up: the wait is called off
        toplevel(comp, manager, 0xff000011, "dosbox", [2])
        assert loop.watches[0](None, None) is True
        seen["timers"] = dict(loop.timers)
        # Gone again, but back when the time is up
        comp.event(0xff000011, 6)
        assert loop.watches[0](None, None) is True
        toplevel(comp, manager, 0xff000012, "dosbox", [])
        assert loop.watches[0](None, None) is True
        loop.fire()
        seen["quits"] = loop.quits
    code, loop = run_main(mod, monkeypatch, comp, script)
    assert code == 0 and seen == {"timers": {}, "quits": 0}
    assert "is gone" not in capsys.readouterr().err


def test_main_ends_on_a_signal_and_lets_go_of_keys(mod, monkeypatch, comp):
    state = {}

    def script(loop):
        sig, quit_ = loop.signals[0]
        comp.requests()
        # Pretend a finger holds Ctrl
        state["keys"] = keys = mod_keys(loop)
        keys.press(29)
        assert quit_() is False
    holder = {}
    real_keys = mod.Keys

    class Keys(real_keys):
        def __init__(self, keyboard):
            super().__init__(keyboard)
            holder["keys"] = self

    def mod_keys(loop):
        return holder["keys"]
    monkeypatch.setattr(mod, "Keys", Keys)
    code, loop = run_main(mod, monkeypatch, comp, script, session=None)
    assert code == 0 and loop.quits == 1
    vk = 6
    assert [r for r in comp.requests() if r[0] == vk and r[1] == 1][-1][2][4:] == u32(29, 0)
    assert holder["keys"].count == {}


def test_main_without_a_window_list_keeps_the_controls(mod, monkeypatch, comp, capsys):
    def script(loop):
        pass
    globals_ = Compositor.GLOBALS[:2]
    code, loop = run_main(mod, monkeypatch, comp, script, globals_=globals_)
    assert code == 0
    assert "no window list: the controls stay" in capsys.readouterr().err


def test_main_ends_when_the_compositor_hangs_up_or_breaks(mod, monkeypatch, comp, capsys):
    def script(loop):
        comp.server.sendall(u32(1, 4 << 16))
        assert loop.watches[0](None, None) is False
    code, loop = run_main(mod, monkeypatch, comp, script)
    assert code == 1 and "broken message" in capsys.readouterr().err

    comp2 = Compositor()
    try:
        def script2(loop):
            comp2.server.shutdown(socket.SHUT_WR)
            assert loop.watches[0](None, None) is False
        code, loop = run_main(mod, monkeypatch, comp2, script2)
        assert code == 1
    finally:
        comp2.close()


def test_main_without_layer_shell_keyboard_or_screen(mod, monkeypatch, comp, capsys):
    gtk = fakes.namespace("Gtk")
    gtk.init = lambda argv: None
    monkeypatch.setattr(mod, "Gtk", gtk)
    monkeypatch.setattr(mod, "GtkLayerShell", Shell(supported=False))
    assert mod.main([]) == 1
    assert "no layer shell" in capsys.readouterr().err

    monkeypatch.setattr(mod, "GtkLayerShell", Shell())

    def refused(cls, env=None):
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(mod.Wire, "connect", classmethod(refused))
    assert mod.main([]) == 1
    assert "no virtual keyboard: refused" in capsys.readouterr().err

    code, loop = run_main(mod, monkeypatch, comp, lambda loop: None,
                          globals_=(("wl_seat", 7),))
    assert code == 1
    assert "no zwp_virtual_keyboard_manager_v1" in capsys.readouterr().err

    comp2 = Compositor()
    try:
        code, loop = run_main(mod, monkeypatch, comp2, lambda loop: None, monitors=[])
        assert code == 1 and "no screen" in capsys.readouterr().err
    finally:
        comp2.close()


def test_program_runs_main(mod, monkeypatch, capsys):
    shell = types.SimpleNamespace(is_supported=lambda: False)
    gtk = fakes.namespace("Gtk")
    gtk.init = lambda argv: None
    code = run_as_main(TOUCH, Gtk=gtk, Gdk=fakes.namespace("Gdk"), GtkLayerShell=shell)
    assert code == 1
