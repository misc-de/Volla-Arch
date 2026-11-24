#!/usr/bin/env python3
"""Send AT commands to the modem through atcid in the Android container.
Usage: modem-at.py (on the phone, as root) 'AT+CMD1' 'AT+CMD2' ...  (one reply block per command)"""
import socket, subprocess, sys, time

pid = subprocess.check_output(["lxc-info", "-n", "android", "-pH"], text=True).strip()
path = f"/proc/{pid}/root/dev/socket/adb_atci_socket"

for cmd in sys.argv[1:]:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path)
    s.sendall((cmd + "\r\n").encode())
    s.settimeout(3)
    out = b""
    end = time.time() + 5
    while time.time() < end:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        out += chunk
        # Stop once the final result code arrived
        if any(k in out for k in (b"OK", b"ERROR")):
            break
    s.close()
    print(f">> {cmd}\n{out.decode(errors='replace').strip()}\n")
