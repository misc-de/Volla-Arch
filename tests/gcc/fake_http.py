# A web server on 127.0.0.1 for the panel tests that load something from the network
# (tests/gcc/test_gcc_software.py: screenshots, test_gcc_webapps.py: a website's page, manifest
# and icons). Nothing leaves the machine: the panels only ever get addresses of this server.
#
# server = LocalWeb(); server.route("/x", body, status=200, headers={...}, delay=0.0)
# server.requests: the paths asked for, in order; server.url("/x") is the full address.

import http.server
import struct
import threading
import time
import zlib


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        web = self.server.web
        with web.lock:
            web.requests.append(self.path)
            route = web.routes.get(self.path, web.routes.get(self.path.split("?")[0]))
        if route is None:
            route = (404, b"not found", {"Content-Type": "text/plain"}, 0.0)
        status, body, headers, delay = route
        if delay:
            time.sleep(delay)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class LocalWeb:
    def __init__(self, host="127.0.0.1"):
        """host: another loopback address (127.0.0.2, ...) for a second server."""
        self.host = host
        self.routes = {}
        self.requests = []
        self.lock = threading.Lock()
        self.httpd = http.server.ThreadingHTTPServer((host, 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.web = self
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def route(self, path, body, status=200, headers=None, delay=0.0, content_type=None):
        if isinstance(body, str):
            body = body.encode()
        headers = dict(headers or {})
        if content_type:
            headers["Content-Type"] = content_type
        with self.lock:
            self.routes[path] = (status, body, headers, delay)

    def redirect(self, path, to, status=302):
        self.route(path, b"", status=status, headers={"Location": to})

    def url(self, path="/"):
        return f"http://{self.host}:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def png(width, height, rgba=(255, 0, 0, 255)):
    """A PNG of one colour."""
    pixel = bytes(rgba)
    raw = b"".join(b"\x00" + pixel * width for _ in range(height))

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def png_header_only(width, height):
    """The start of a PNG that claims width x height (no pixel data)."""
    data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(data)) + b"IHDR" + data
            + struct.pack(">I", zlib.crc32(b"IHDR" + data) & 0xffffffff))
