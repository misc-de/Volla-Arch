# A browser for gtk4-broadwayd that shows nothing (tests/gcc, see gcc_harness.py).
#
# GTK draws a frame, then waits until the browser says it got it (a "roundtrip"): without a browser
# the frame clock stays frozen after the first frame, so nothing is laid out again (an opened dialog
# keeps its content unmapped, lists get no rows). This client connects to broadwayd's HTTP unix
# socket, upgrades to its WebSocket and answers every roundtrip and resize, the way broadway.js
# (served by broadwayd itself) does; the drawing commands are skipped.
#
# Usage: python3 -I gcc_broadway_client.py HTTP_SOCKET  (prints READY once connected)

import base64
import os
import select
import socket
import struct
import sys
import time

# broadway.js's numbers
(OP_GRAB_POINTER, OP_UNGRAB_POINTER, OP_NEW_SURFACE, OP_SHOW_SURFACE, OP_HIDE_SURFACE,
 OP_RAISE_SURFACE, OP_LOWER_SURFACE, OP_DESTROY_SURFACE, OP_MOVE_RESIZE, OP_SET_TRANSIENT_FOR,
 OP_DISCONNECTED, OP_SURFACE_UPDATE, OP_SET_SHOW_KEYBOARD, OP_UPLOAD_TEXTURE, OP_RELEASE_TEXTURE,
 OP_SET_NODES, OP_ROUNDTRIP) = range(17)

EVENT_CONFIGURE_NOTIFY = 11
EVENT_SCREEN_SIZE_CHANGED = 12
EVENT_ROUNDTRIP_NOTIFY = 14

START = time.monotonic()
FRAME_S = 0.001
last_serial = 0
surfaces = {}
pending = []  # roundtrips to answer: (when, serial, surface, tag)


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def send_frame(sock, payload, opcode=2):
    # A client's frames are masked (RFC 6455); the mask is zero, which is allowed
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    sock.sendall(head + b"\0\0\0\0" + payload)


def send_input(sock, event, args, serial=None):
    ts = int((time.monotonic() - START) * 1000)
    send_frame(sock, struct.pack(">" + "i" * (3 + len(args)), event,
                                 last_serial if serial is None else serial, ts, *args))


def read_message(sock):
    """One message (fragments joined); None for a close."""
    data = b""
    while True:
        b0, b1 = recv_exact(sock, 2)
        opcode = b0 & 0x0F
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", recv_exact(sock, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", recv_exact(sock, 8))[0]
        mask = recv_exact(sock, 4) if b1 & 0x80 else None
        payload = recv_exact(sock, n)
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        if opcode == 8:
            return None
        if opcode == 9:
            send_frame(sock, payload, opcode=10)
            continue
        data += payload
        if b0 & 0x80:
            return data


def handle(sock, msg):
    """broadway.js's handleCommands, keeping only what needs an answer."""
    global last_serial
    pos = 0

    def take(fmt):
        nonlocal pos
        values = struct.unpack_from("<" + fmt, msg, pos)
        pos += struct.calcsize("<" + fmt)
        return values

    while pos < len(msg):
        op, = take("B")
        last_serial, = take("I")
        if op == OP_NEW_SURFACE:
            sid, x, y, w, h = take("HhhHH")
            surfaces[sid] = [x, y, w, h]
        elif op in (OP_SHOW_SURFACE, OP_HIDE_SURFACE, OP_RAISE_SURFACE, OP_LOWER_SURFACE,
                    OP_DESTROY_SURFACE):
            sid, = take("H")
            if op == OP_DESTROY_SURFACE:
                surfaces.pop(sid, None)
        elif op == OP_SET_TRANSIENT_FOR:
            take("HH")
        elif op == OP_ROUNDTRIP:
            sid, tag = take("HI")
            # Answered a little later, as a display would: a widget that animates for ever (a
            # spinner) gets frames at a display's pace, not as fast as the CPU can draw them
            pending.append((time.monotonic() + FRAME_S, last_serial, sid, tag))
        elif op == OP_MOVE_RESIZE:
            sid, flags = take("HB")
            s = surfaces.setdefault(sid, [0, 0, 0, 0])
            if flags & 1:
                s[0], s[1] = take("hh")
            if flags & 2:
                s[2], s[3] = take("HH")
            send_input(sock, EVENT_CONFIGURE_NOTIFY, [sid, *s])
        elif op == OP_UPLOAD_TEXTURE:
            _tid, size = take("II")
            pos += size
        elif op == OP_RELEASE_TEXTURE:
            take("I")
        elif op == OP_SET_NODES:
            _sid, words = take("HI")
            pos += words * 4
        elif op == OP_GRAB_POINTER:
            take("HB")
        elif op == OP_UNGRAB_POINTER:
            pass
        elif op == OP_SET_SHOW_KEYBOARD:
            take("H")
        elif op == OP_DISCONNECTED:
            return False
        else:
            # Unknown: the rest of the message cannot be read
            print(f"gcc_broadway_client: unknown op {op}", file=sys.stderr)
            return True
    return True


def main(path):
    sock = socket.socket(socket.AF_UNIX)
    for _ in range(500):
        try:
            sock.connect(path)
            break
        except OSError:
            time.sleep(0.01)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(("GET /socket HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Protocol: broadway\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        head += recv_exact(sock, 1)
    if b" 101 " not in head.split(b"\r\n", 1)[0]:
        sys.exit(f"gcc_broadway_client: no WebSocket: {head!r}")
    # A screen as large as a desktop's: the test windows are phone-sized
    send_input(sock, EVENT_SCREEN_SIZE_CHANGED, [1280, 1024, 1])
    print("READY", flush=True)
    while True:
        timeout = max(0.0, pending[0][0] - time.monotonic()) if pending else None
        readable, _, _ = select.select([sock], [], [], timeout)
        if readable:
            try:
                msg = read_message(sock)
            except EOFError:
                return
            if msg is None or not handle(sock, msg):
                return
        now = time.monotonic()
        while pending and pending[0][0] <= now:
            _when, serial, sid, tag = pending.pop(0)
            send_input(sock, EVENT_ROUNDTRIP_NOTIFY, [sid, tag], serial=serial)


if __name__ == "__main__":
    main(sys.argv[1])
