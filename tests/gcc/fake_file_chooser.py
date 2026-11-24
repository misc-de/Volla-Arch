# A fake file chooser portal (org.freedesktop.portal.FileChooser of org.freedesktop.portal.Desktop)
# on the test's private session bus (tests/gcc): GtkFileDialog asks it (GDK_DEBUG=portals) and
# gets the answers a test gives, sent as the portal sends them, with the Response signal of the
# request object that the call names (handle_token and the caller's bus name).
#
# In a test:  FileChooserPortal(gcc_world, [{"uris": ["file:///x.pem"]}, {"response": 1}]).start()
# before gcc_world.run. An answer is {"uris": [...]} (chosen), {"response": 1} (dismissed),
# {"response": 2} (ended otherwise) or {"never": true} (no answer); "delay_ms" waits before it. The last
# answer stays for further calls. Calls go into the world's call log with name
# "org.freedesktop.portal.Desktop" (OpenFile with its options, Close of a request).
#
# As a program: python3 -I fake_file_chooser.py SPEC.json (DBUS_SESSION_BUS_ADDRESS names the bus).

import json
import os
import subprocess
import sys
from pathlib import Path

NAME = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
XML = """<node><interface name="org.freedesktop.portal.FileChooser">
 <method name="OpenFile"><arg type="s" name="parent_window" direction="in"/>
  <arg type="s" name="title" direction="in"/><arg type="a{sv}" name="options" direction="in"/>
  <arg type="o" name="handle" direction="out"/></method>
 <method name="SaveFile"><arg type="s" name="parent_window" direction="in"/>
  <arg type="s" name="title" direction="in"/><arg type="a{sv}" name="options" direction="in"/>
  <arg type="o" name="handle" direction="out"/></method>
 <property name="version" type="u" access="read"/>
</interface></node>"""
REQUEST_XML = """<node><interface name="org.freedesktop.portal.Request">
 <method name="Close"/>
 <signal name="Response"><arg type="u" name="response"/><arg type="a{sv}" name="results"/></signal>
</interface></node>"""


class FileChooserPortal:
    def __init__(self, world, answers):
        self.world = world
        self.answers = answers
        self.proc = None

    def start(self):
        if self.proc:
            return
        self.world.start()
        spec = self.world.tmp / "fake-file-chooser.json"
        spec.write_text(json.dumps({"log": str(self.world.call_log), "answers": self.answers}))
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=self.world.buses["session"])
        err = self.world.tmp / "fake-file-chooser.err"
        with open(err, "w") as stderr:
            self.proc = subprocess.Popen(["python3", "-I", str(Path(__file__).resolve()), str(spec)],
                                         env=env, stdout=subprocess.PIPE, stderr=stderr, text=True)
        # World.close() ends it with the buses
        self.world.procs.insert(0, self.proc)
        line = self.proc.stdout.readline()
        if line.strip() != "READY":
            raise AssertionError(f"fake file chooser did not start: {line}{err.read_text()}")


def serve(spec):
    from gi.repository import Gio, GLib

    log_fd = os.open(spec["log"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    answers = list(spec["answers"])
    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
    request_info = Gio.DBusNodeInfo.new_for_xml(REQUEST_XML).interfaces[0]

    def log(path, iface, member, args):
        def plain(v):
            if isinstance(v, (list, tuple)):
                return [plain(x) for x in v]
            if isinstance(v, dict):
                return {str(k): plain(x) for k, x in v.items()}
            if isinstance(v, bytes):
                return list(v)
            return v
        record = {"bus": "session", "name": NAME, "path": path, "interface": iface, "member": member,
                  "args": plain(args)}
        os.write(log_fd, (json.dumps(record) + "\n").encode())

    def on_request(c, sender, path, iface, method, params, invocation):
        log(path, iface, method, [])
        invocation.return_value(None)

    def respond(sender, handle, answer):
        if "uris" in answer:
            value = GLib.Variant("(ua{sv})", (0, {"uris": GLib.Variant("as", answer["uris"])}))
        else:
            value = GLib.Variant("(ua{sv})", (answer["response"], {}))
        # Sent to the caller only, as xdg-desktop-portal does (GTK adds no match rule for it)
        conn.emit_signal(sender, handle, "org.freedesktop.portal.Request", "Response", value)
        return False

    def on_call(c, sender, path, iface, method, params, invocation):
        args = params.unpack()
        log(path, iface, method, args)
        token = args[2].get("handle_token", "t")
        handle = f"{PATH}/request/{sender[1:].replace('.', '_')}/{token}"
        try:
            conn.register_object(handle, request_info, on_request, None, None)
        except GLib.Error:
            pass
        answer = answers[0]
        if len(answers) > 1:
            answers.pop(0)
        invocation.return_value(GLib.Variant("(o)", (handle,)))
        if not answer.get("never"):
            GLib.timeout_add(answer.get("delay_ms", 20), respond, sender, handle, answer)

    def on_get(c, sender, path, iface, prop):
        return GLib.Variant("u", 4) if prop == "version" else None

    conn.register_object(PATH, info, on_call, on_get, None)
    loop = GLib.MainLoop()
    Gio.bus_own_name_on_connection(conn, NAME, Gio.BusNameOwnerFlags.NONE,
                                   lambda *_: print("READY", flush=True), lambda *_: loop.quit())
    loop.run()


if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        serve(json.load(f))
