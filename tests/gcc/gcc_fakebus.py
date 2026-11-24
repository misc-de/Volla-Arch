# Fake D-Bus services for the GNOME Control Center panel tests (tests/gcc, see gcc_harness.py).
#
# Usage: python3 -I gcc_fakebus.py SPEC.json  (DBUS_SYSTEM_BUS_ADDRESS and DBUS_SESSION_BUS_ADDRESS
# name the test's private buses). SPEC: {"log": FILE, "services": [{"bus", "name", "path", "xml",
# "properties": {prop: GVariant text}, "replies": {method: reply}, "own": bool}]}; a reply is GVariant
# text of the out arguments (a tuple), {"error": name, "message": text}, {"portal_request": {...}}
# (a portal's Request answered with a Response signal, see Obj._portal_request), or a list of those
# for successive calls (the last one stays). Every call (and every property Set) is appended to FILE as
# a JSON line. org.plinius.TestFake at /org/plinius/TestFake on each bus lets the test's script
# make a service emit a signal, change a property, change a reply, leave or come back.
# Prints READY once every name is owned.

import json
import sys

from gi.repository import Gio, GLib

CONTROL_XML = """<node><interface name="org.plinius.TestFake">
 <method name="Emit"><arg type="s" direction="in"/><arg type="s" direction="in"/>
  <arg type="s" direction="in"/><arg type="s" direction="in"/></method>
 <method name="SetProperty"><arg type="s" direction="in"/><arg type="s" direction="in"/>
  <arg type="s" direction="in"/><arg type="s" direction="in"/></method>
 <method name="SetReply"><arg type="s" direction="in"/><arg type="s" direction="in"/>
  <arg type="s" direction="in"/><arg type="s" direction="in"/></method>
 <method name="EmitRaw"><arg type="s" direction="in"/><arg type="s" direction="in"/>
  <arg type="s" direction="in"/><arg type="s" direction="in"/></method>
 <method name="Release"><arg type="s" direction="in"/></method>
 <method name="Own"><arg type="s" direction="in"/></method>
</interface></node>"""


def split_types(sig):
    """The complete types a signature string holds, in order."""
    out, i = [], 0
    while i < len(sig):
        j = i
        while sig[j] in "am":
            j += 1
        if sig[j] in "({":
            depth, k = 0, j
            while True:
                if sig[k] in "({":
                    depth += 1
                elif sig[k] in ")}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            j = k
        out.append(sig[i:j + 1])
        i = j + 1
    return out


def zero_value(sig):
    if sig.startswith("a{"):
        return {}
    if sig[0] in "am":
        return [] if sig[0] == "a" else None
    if sig[0] in "({":
        return tuple(zero_value(t) for t in split_types(sig[1:-1]))
    if sig == "v":
        return GLib.Variant("s", "")
    if sig in "sg":
        return ""
    if sig == "o":
        return "/"
    if sig == "b":
        return False
    if sig == "d":
        return 0.0
    return 0


def zero(sig):
    """A GVariant of type sig with zero values."""
    return GLib.Variant(sig, zero_value(sig))


def out_signature(method_info):
    return "(" + "".join(a.signature for a in method_info.out_args) + ")"


def unpack(v):
    if v is None:
        return None
    return v.unpack()


def jsonable(v):
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, bytes):
        return list(v)
    return v


class Obj:
    def __init__(self, fake, spec):
        self.fake = fake
        self.spec = spec
        self.node = Gio.DBusNodeInfo.new_for_xml(spec["xml"])
        self.props = {}
        self.replies = {}
        for iface in self.node.interfaces:
            for p in iface.properties:
                text = spec["properties"].get(p.name)
                self.props[(iface.name, p.name)] = (GLib.Variant.parse(GLib.VariantType.new(p.signature), text, None, None)
                                                    if text is not None else zero(p.signature))
            for m in iface.methods:
                r = spec["replies"].get(m.name)
                if r is not None:
                    self.replies[(iface.name, m.name)] = r if isinstance(r, list) else [r]
        self.reg_ids = []

    def register(self, conn):
        for iface in self.node.interfaces:
            self.reg_ids.append(conn.register_object(self.spec["path"], iface, self.on_call,
                                                     self.on_get, self.on_set))

    def iface(self, name):
        return next(i for i in self.node.interfaces if i.name == name)

    def log(self, conn, sender, iface, member, args):
        self.fake.log({"bus": self.spec["bus"], "name": self.spec["name"], "path": self.spec["path"],
                       "interface": iface, "member": member, "args": jsonable(args)})

    def on_call(self, conn, sender, path, iface, method, params, invocation):
        try:
            self._on_call(conn, sender, path, iface, method, params, invocation)
        except Exception:  # a broken spec of the test: the panel gets an error, stderr says why
            import traceback
            traceback.print_exc()
            invocation.return_dbus_error("org.plinius.TestFake.Error", traceback.format_exc())

    def _on_call(self, conn, sender, path, iface, method, params, invocation):
        self.log(conn, sender, iface, method, unpack(params))
        info = self.iface(iface).lookup_method(method)
        queue = self.replies.get((iface, method))
        reply = None
        if queue:
            reply = queue[0]
            if len(queue) > 1:
                queue.pop(0)
        if isinstance(reply, dict) and "error" in reply:
            invocation.return_dbus_error(reply["error"], reply.get("message", ""))
            return
        if isinstance(reply, dict) and "delay_ms" in reply:
            value = reply.get("reply")
            GLib.timeout_add(reply["delay_ms"], self._reply_later, invocation, info, value)
            return
        if isinstance(reply, dict) and "portal_request" in reply:
            self._portal_request(conn, sender, params, invocation, reply["portal_request"])
            return
        self._reply(invocation, info, reply)

    def _portal_request(self, conn, sender, params, invocation, answer):
        """A method of an xdg-desktop-portal frontend that answers with a Request object: the
        handle is the path the caller expects (its unique name and the options' handle_token),
        org.freedesktop.portal.Request.Response follows there. answer: {"response": u,
        "results": GVariant text of a{sv}, "delay_ms": n}."""
        token = "t"
        for arg in params.unpack():
            if isinstance(arg, dict) and "handle_token" in arg:
                token = arg["handle_token"]
        handle = f"/org/freedesktop/portal/desktop/request/{sender[1:].replace('.', '_')}/{token}"
        invocation.return_value(GLib.Variant("(o)", (handle,)))

        def respond():
            results = GLib.Variant.parse(GLib.VariantType.new("a{sv}"), answer.get("results", "@a{sv} {}"),
                                         None, None)
            conn.emit_signal(sender, handle, "org.freedesktop.portal.Request", "Response",
                             GLib.Variant.new_tuple(GLib.Variant("u", answer.get("response", 0)), results))
            return False

        GLib.timeout_add(answer.get("delay_ms", 20), respond)

    def _reply(self, invocation, info, text):
        sig = out_signature(info)
        value = GLib.Variant.parse(GLib.VariantType.new(sig), text, None, None) if text is not None else zero(sig)
        invocation.return_value(value)

    def _reply_later(self, invocation, info, text):
        self._reply(invocation, info, text)
        return False

    def on_get(self, conn, sender, path, iface, prop):
        return self.props.get((iface, prop))

    def on_set(self, conn, sender, path, iface, prop, value):
        self.log(conn, sender, "org.freedesktop.DBus.Properties", "Set", [iface, prop, unpack(value)])
        self.change(conn, iface, prop, value)
        return True

    def change(self, conn, iface, prop, value):
        self.props[(iface, prop)] = value
        conn.emit_signal(None, self.spec["path"], "org.freedesktop.DBus.Properties", "PropertiesChanged",
                         GLib.Variant("(sa{sv}as)", (iface, {prop: value}, [])))


class Fake:
    def __init__(self, spec):
        self.spec = spec
        self.logf = open(spec["log"], "a", buffering=1)
        import threading
        self.lock = threading.Lock()
        self.conns = {}
        self.objs = [Obj(self, s) for s in spec["services"]]
        self.owners = {}
        self.pending = 0
        self.loop = GLib.MainLoop()

    def log(self, record):
        with self.lock:
            self.logf.write(json.dumps(record) + "\n")

    def refuse_sets(self, conn, message, incoming):
        """A Set of a property in a service's set_errors is answered with that error (GDBus' own
        property handling can only refuse with a message of its own)."""
        if (not incoming or message.get_message_type() != Gio.DBusMessageType.METHOD_CALL
                or message.get_interface() != "org.freedesktop.DBus.Properties"
                or message.get_member() != "Set"):
            return message
        iface, prop, value = message.get_body().unpack()
        for o in self.objs:
            error = o.spec.get("set_errors", {}).get(prop)
            if error and o.spec["path"] == message.get_path() and conn is self.conns[o.spec["bus"]]:
                o.log(conn, message.get_sender(), "org.freedesktop.DBus.Properties", "Set",
                      [iface, prop, jsonable(value)])
                conn.send_message(message.new_method_error_literal(error[0], error[1]),
                                  Gio.DBusSendMessageFlags.NONE)
                return None
        return message

    def on_control(self, conn, sender, path, iface, method, params, invocation):
        args = params.unpack()
        # In the log too: a test can tell what came before and after
        self.log({"bus": next(k for k, c in self.conns.items() if c is conn), "name": "org.plinius.TestFake",
                  "path": path, "interface": iface, "member": method, "args": jsonable(args)})
        try:
            if method == "Emit":
                opath, oiface, signal, text = args
                obj = self.find(conn, opath, oiface)
                sinfo = obj.iface(oiface).lookup_signal(signal)
                sig = "(" + "".join(a.signature for a in sinfo.args) + ")"
                conn.emit_signal(None, opath, oiface, signal,
                                 GLib.Variant.parse(GLib.VariantType.new(sig), text, None, None))
            elif method == "EmitRaw":
                # Any arguments, not those of the interface: a service that sends something odd
                opath, oiface, signal, text = args
                conn.emit_signal(None, opath, oiface, signal, GLib.Variant.parse(None, text, None, None))
            elif method == "SetProperty":
                opath, oiface, prop, text = args
                obj = self.find(conn, opath, oiface)
                pinfo = obj.iface(oiface).lookup_property(prop)
                obj.change(conn, oiface, prop,
                           GLib.Variant.parse(GLib.VariantType.new(pinfo.signature), text, None, None))
            elif method == "SetReply":
                opath, oiface, m, text = args
                obj = self.find(conn, opath, oiface)
                r = json.loads(text)
                obj.replies[(oiface, m)] = r if isinstance(r, list) else [r]
            elif method == "Release":
                owner = self.owners.pop((id(conn), args[0]), None)
                if owner:
                    Gio.bus_unown_name(owner)
                conn.flush_sync(None)
            elif method == "Own":
                self.own(conn, args[0], None)
            invocation.return_value(None)
        except Exception as e:  # the test's script asked for something impossible
            invocation.return_dbus_error("org.plinius.TestFake.Error", repr(e))

    def find(self, conn, path, iface):
        bus = next(k for k, c in self.conns.items() if c is conn)
        for o in self.objs:
            if o.spec["bus"] == bus and o.spec["path"] == path and any(i.name == iface for i in o.node.interfaces):
                return o
        raise KeyError(f"no fake {iface} at {path} on {bus}")

    def own(self, conn, name, done):
        self.pending += 1

        def acquired(*_):
            self.pending -= 1
            if done:
                done()

        def lost(*_):
            print(f"gcc_fakebus: lost {name}", file=sys.stderr)

        self.owners[(id(conn), name)] = Gio.bus_own_name_on_connection(conn, name, Gio.BusNameOwnerFlags.NONE,
                                                                        acquired, lost)

    def run(self):
        control = Gio.DBusNodeInfo.new_for_xml(CONTROL_XML).interfaces[0]
        for bus in ("system", "session"):
            conn = Gio.bus_get_sync(Gio.BusType.SYSTEM if bus == "system" else Gio.BusType.SESSION, None)
            self.conns[bus] = conn
            conn.add_filter(self.refuse_sets)
            conn.register_object("/org/plinius/TestFake", control, self.on_control, None, None)
        for o in self.objs:
            o.register(self.conns[o.spec["bus"]])
        names = {(o.spec["bus"], o.spec["name"]) for o in self.objs if o.spec.get("own", True)}
        names |= {("system", "org.plinius.TestFake"), ("session", "org.plinius.TestFake")}
        waiting = [len(names)]

        def one_done():
            waiting[0] -= 1
            if waiting[0] == 0:
                print("READY", flush=True)

        for bus, name in sorted(names):
            self.own(self.conns[bus], name, one_done)

        # A panel that signals a service (feedbackd reloads its theme on SIGHUP) signals the fake,
        # the owner of the name: logged as {"name": "signal", "member": "SIGHUP"}, not the end
        def on_sighup():
            self.log({"bus": None, "name": "signal", "path": None, "interface": None, "member": "SIGHUP",
                      "args": []})
            return GLib.SOURCE_CONTINUE

        try:
            from gi.repository import GLibUnix
            GLibUnix.signal_add(GLib.PRIORITY_HIGH, 1, on_sighup)  # 1: SIGHUP
        except ImportError:  # PyGObject before GLib 2.80
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, 1, on_sighup)
        self.loop.run()


if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        Fake(json.load(f)).run()
