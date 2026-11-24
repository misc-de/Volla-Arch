# ofono2mm as built (pinned commit + our patches, from the source cache): who may use the modem.
# authorization.py (patches 0018, 0021) decides which ModemManager call needs which polkit
# action and who may make an emergency call without polkit. The structural tests keep a new
# upstream method or a writable property from bypassing it.

import ast
import shutil

import pytest

from conftest import load_script
from prepare_helper import SRCDEST, Unavailable, run_prepare

pytestmark = pytest.mark.skipif(not SRCDEST.is_dir(), reason="no source cache")

PREFIX = "org.freedesktop.ModemManager1."


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    work = tmp_path_factory.mktemp("ofono2mm")
    try:
        r = run_prepare("ofono2mm", work)
    except Unavailable as e:
        shutil.rmtree(work, ignore_errors=True)
        pytest.skip(str(e))
    assert r.returncode == 0, r.stdout + r.stderr
    yield work / "src" / "oFono2MM"
    shutil.rmtree(work, ignore_errors=True)


@pytest.fixture(scope="module")
def auth(tree):
    return load_script(tree / "ofono2mm" / "authorization.py", "ofono2mm_authorization")


class V:
    """dbus-next's Variant as far as authorization.py looks at it."""

    def __init__(self, value):
        self.value = value


# --- which call needs which action -------------------------------------------------------

@pytest.mark.parametrize("interface,member,path,action", [
    ("org.freedesktop.ModemManager1", "ScanDevices", "/org/freedesktop/ModemManager1", "Control"),
    (PREFIX + "Modem", "Enable", "/m/0", "Device.Control"),
    (PREFIX + "Modem", "Command", "/m/0", "Device.Control"),
    (PREFIX + "Sim", "SendPuk", "/s/0", "Device.Control"),
    (PREFIX + "Modem.Location", "GetLocation", "/m/0", "Location"),
    (PREFIX + "Modem.Location", "Setup", "/m/0", "Device.Control"),
    (PREFIX + "Modem.Messaging", "List", "/m/0", "Messaging"),
    (PREFIX + "Sms", "Send", "/org/freedesktop/ModemManager1/SMS/1", "Messaging"),
    (PREFIX + "Modem.Voice", "CreateCall", "/m/0", "Voice"),
    (PREFIX + "Call", "Hangup", "/org/freedesktop/ModemManager1/Call/1", "Voice"),
    (PREFIX + "Modem.Modem3gpp.Ussd", "Initiate", "/m/0", "USSD"),
    (PREFIX + "Modem.CellBroadcast", "SetChannels", "/m/0", "CellBroadcast"),
    (PREFIX + "Modem.Firmware", "List", "/m/0", "Firmware"),
    (PREFIX + "Modem.Time", "GetNetworkTime", "/m/0", "Time"),
    # Sender and text of an SMS, the number of a caller: private like the lists of them
    ("org.freedesktop.DBus.Properties", "GetAll", "/org/freedesktop/ModemManager1/SMS/3",
     "Messaging"),
    ("org.freedesktop.DBus.Properties", "Get", "/org/freedesktop/ModemManager1/Call/2", "Voice"),
])
def test_required_action(auth, interface, member, path, action):
    assert auth.required_action(interface, member, path) == PREFIX + action


@pytest.mark.parametrize("interface,member,path", [
    ("org.freedesktop.DBus.Properties", "GetAll", "/org/freedesktop/ModemManager1/Modem/0"),
    ("org.freedesktop.DBus.Properties", "Get", None),
    ("org.freedesktop.DBus.Introspectable", "Introspect", "/"),
    ("org.freedesktop.DBus.ObjectManager", "GetManagedObjects", "/org/freedesktop/ModemManager1"),
    (PREFIX + "Modem.Location", "NotAMethod", "/m/0"),
])
def test_calls_without_action(auth, interface, member, path):
    assert auth.required_action(interface, member, path) is None


def test_similar_path_prefix_is_not_an_sms(auth):
    # Only objects below SMS/ are messages; a path that merely starts alike is not
    assert auth.required_action("org.freedesktop.DBus.Properties", "Get",
                                "/org/freedesktop/ModemManager1/SMSC") is None


# --- emergency numbers ---------------------------------------------------------------------

@pytest.mark.parametrize("number,known,expected", [
    ("112", (), True),
    ("911", (), True),
    (" 1 1 2 ", (), True),
    ("1-1-2", (), True),
    ("110", (), False),
    ("110", ("110", "112"), True),
    ("110", ("1 10",), True),          # the SIM's list is normalized too
    ("1120", (), False),
    ("0112", (), False),
    ("+49112", (), False),
    ("", ("",), False),                 # an empty number is never an emergency call
    ("---", (), False),
    (112, (), True),                    # a number that came as an integer
])
def test_is_emergency_number(auth, number, known, expected):
    assert auth.is_emergency_number(number, known) is expected


def test_create_call_to_emergency_number_skips_polkit(auth):
    body = [{"number": V("112")}]
    assert auth.is_emergency_request(PREFIX + "Modem.Voice", "CreateCall", body, None, [], ())


@pytest.mark.parametrize("body", [[], [{}], [{"number": "112"}], None, [None]])
def test_malformed_create_call_is_no_emergency(auth, body):
    # A malformed request is treated as an ordinary call (polkit decides), not let through
    assert not auth.is_emergency_request(PREFIX + "Modem.Voice", "CreateCall", body, None, [],
                                         ())


def test_voice_requests_skip_polkit_only_while_an_emergency_call_runs(auth):
    voice = PREFIX + "Modem.Voice"
    assert auth.is_emergency_request(voice, "HangupAll", [], None, ["0301234", "112"], ())
    assert not auth.is_emergency_request(voice, "HangupAll", [], None, ["0301234"], ())
    assert not auth.is_emergency_request(voice, "HangupAll", [], None, [], ())


def test_call_requests_skip_polkit_only_for_the_emergency_call(auth):
    call = PREFIX + "Call"
    assert auth.is_emergency_request(call, "Hangup", [], "112", ["112"], ())
    assert not auth.is_emergency_request(call, "Hangup", [], "0301234", ["112"], ())
    assert not auth.is_emergency_request(call, "Hangup", [], None, ["112"], ())


def test_other_interfaces_are_never_emergency_requests(auth):
    body = [{"number": V("112")}]
    assert not auth.is_emergency_request(PREFIX + "Modem.Messaging", "Create", body, None,
                                         ["112"], ())


# --- who may handle an emergency call ------------------------------------------------------

def test_may_handle_emergency(auth):
    never = lambda uid: False  # noqa: E731
    assert auth.may_handle_emergency(0, active_uid=1000, member=never)
    assert auth.may_handle_emergency(1000, active_uid=1000, member=never)
    assert not auth.may_handle_emergency(1001, active_uid=1000, member=never)
    assert not auth.may_handle_emergency(65534, active_uid=1000, member=never)  # "nobody"
    assert auth.may_handle_emergency(1001, active_uid=1000, member=lambda uid: uid == 1001)
    assert not auth.may_handle_emergency(None, active_uid=1000, member=lambda uid: True)


@pytest.mark.parametrize("content,uid", [
    ("# This is private data. Do not parse.\nACTIVE=c2\nACTIVE_UID=1000\n", 1000),
    ("ACTIVE=c2\n", None),
    ("ACTIVE_UID=\n", None),
    ("ACTIVE_UID=abc\n", None),
    ("", None),
])
def test_active_seat_uid(auth, tmp_path, content, uid):
    state = tmp_path / "seat0"
    state.write_text(content)
    assert auth.active_seat_uid(str(state)) == uid


def test_active_seat_uid_without_logind(auth, tmp_path):
    assert auth.active_seat_uid(str(tmp_path / "missing")) is None


def test_unknown_uid_is_in_no_group(auth):
    assert auth.in_group(2 ** 31 - 2) is False


# --- remembered permissions ----------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_allow_cache_expires(auth):
    clock = Clock()
    cache = auth.AllowCache(lifetime=60, clock=clock)
    assert not cache.allowed(":1.5", "voice")
    cache.allow(":1.5", "voice")
    assert cache.allowed(":1.5", "voice")
    assert not cache.allowed(":1.5", "messaging")
    assert not cache.allowed(":1.6", "voice")
    clock.now += 59.9
    assert cache.allowed(":1.5", "voice")
    clock.now += 0.1
    assert not cache.allowed(":1.5", "voice")
    assert len(cache) == 0


def test_allow_cache_forgets_a_sender_that_left(auth):
    cache = auth.AllowCache(clock=Clock())
    cache.allow(":1.5", "voice")
    cache.allow(":1.5", "messaging")
    cache.allow(":1.7", "voice")
    cache.forget(":1.5")
    assert not cache.allowed(":1.5", "voice") and not cache.allowed(":1.5", "messaging")
    assert cache.allowed(":1.7", "voice")


def test_allow_cache_drops_expired_entries_when_it_grows(auth):
    clock = Clock()
    cache = auth.AllowCache(lifetime=1, clock=clock)
    for i in range(300):
        cache.allow(f":1.{i}", "voice")
    clock.now += 2
    cache.allow(":2.0", "voice")
    assert len(cache) == 1


# --- structure: nothing bypasses the table -------------------------------------------------

def interfaces(tree):
    """{interface name: ([method names], [(property, access)])} of every ServiceInterface."""
    found = {}
    for f in [tree / "main.py", *sorted((tree / "ofono2mm").glob("*.py"))]:
        for cls in ast.walk(ast.parse(f.read_text())):
            if not isinstance(cls, ast.ClassDef):
                continue
            name = None
            for node in ast.walk(cls):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "__init__" and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and str(node.args[0].value).startswith("org.freedesktop.ModemManager1")):
                    name = node.args[0].value
            if name is None:
                continue
            methods, props = [], []
            for fn in cls.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in fn.decorator_list:
                    call = dec if isinstance(dec, ast.Call) else None
                    dname = (call.func if call else dec)
                    dname = getattr(dname, "id", getattr(dname, "attr", ""))
                    if dname == "method":
                        # @method(name="X") exports under another name
                        alias = [k.value.value for k in (call.keywords if call else [])
                                 if k.arg == "name"]
                        methods.append(alias[0] if alias else fn.name)
                    elif dname == "dbus_property":
                        access = [ast.unparse(k.value) for k in (call.keywords if call else [])
                                  if k.arg == "access"]
                        access += [ast.unparse(a) for a in (call.args if call else [])]
                        props.append((fn.name, access[0] if access else "READWRITE (default)"))
            m, p = found.setdefault(name, ([], []))
            m.extend(methods)
            p.extend(props)
    return found


def test_interfaces_are_found(tree):
    found = interfaces(tree)
    assert PREFIX + "Modem.Voice" in found and "CreateCall" in found[PREFIX + "Modem.Voice"][0]
    assert len(found) >= 20


def test_every_method_needs_an_action(tree, auth):
    """A method missing from ACTIONS needs no authorization at all: every local user, even
    "nobody", could call it."""
    free = [f"{iface}.{m}" for iface, (methods, _) in interfaces(tree).items()
            for m in methods if auth.required_action(iface, m, "/") is None]
    assert not free, f"methods without a polkit action: {free}"


def test_no_property_is_writable(tree):
    """Properties.Set is not authorized (required_action returns None for it): a writable
    property would be a way around the table."""
    writable = [f"{iface}.{name} ({access})" for iface, (_, props) in interfaces(tree).items()
                for name, access in props if not access.endswith("PropertyAccess.READ")]
    assert not writable, writable
