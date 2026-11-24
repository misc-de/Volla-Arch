# plinius-login-keyring-unlock (pkgbuilds/plinius-login-keyring): which device key it accepts,
# when it creates, unlocks or leaves the login keyring alone, and that the items of an old
# default keyring are only removed after every copy was read back.
#
# The Secret Service is a fake object (no session bus); key and keyring directories are in
# tmp_path; keys and secrets are dummies.

import os
import socket

import pytest

from conftest import PKG, load_script

SCRIPT = PKG / "plinius-login-keyring" / "plinius-login-keyring-unlock"
BASE = "/org/freedesktop/secrets/collection/"
DUMMY_KEY = b"k" * 40


@pytest.fixture(scope="module")
def mod():
    pytest.importorskip("gi")
    return load_script(SCRIPT)


# --- The device key --------------------------------------------------------------------------

@pytest.fixture
def key_dir(mod, tmp_path, monkeypatch):
    d = tmp_path / "keys"
    d.mkdir()
    monkeypatch.setattr(mod, "KEY_DIR", str(d))
    return d / str(os.getuid())


def test_read_key_accepts_a_private_key(mod, key_dir):
    key_dir.write_bytes(DUMMY_KEY + b"\n")
    key_dir.chmod(0o600)
    assert mod.read_key() == DUMMY_KEY
    key_dir.chmod(0o400)
    assert mod.read_key() == DUMMY_KEY


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620, 0o666])
def test_read_key_refuses_a_key_others_can_access(mod, key_dir, mode, capsys):
    key_dir.write_bytes(DUMMY_KEY)
    key_dir.chmod(mode)
    assert mod.read_key() is None
    assert "refusing device key" in capsys.readouterr().err


def test_read_key_refuses_short_missing_and_linked_keys(mod, key_dir, tmp_path):
    assert mod.read_key() is None
    key_dir.write_bytes(b"x" * 31 + b"\n\n")
    key_dir.chmod(0o600)
    assert mod.read_key() is None
    key_dir.unlink()
    target = tmp_path / "elsewhere"
    target.write_bytes(DUMMY_KEY)
    target.chmod(0o600)
    key_dir.symlink_to(target)
    assert mod.read_key() is None  # O_NOFOLLOW


def test_notify(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    mod.notify("READY=1")  # nothing to tell
    path = tmp_path / "notify"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind(str(path))
        monkeypatch.setenv("NOTIFY_SOCKET", str(path))
        mod.notify("READY=1")
        assert sock.recv(64) == b"READY=1"
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "gone"))
    mod.notify("READY=1")
    assert "could not notify" in capsys.readouterr().err


# --- A fake Secret Service --------------------------------------------------------------------

class FakeSecrets:
    """Collections as {path: {"label", "locked", "password", "items": {path: item}}}."""

    def __init__(self, mod, collections, default="/"):
        self.mod, self.cols, self.default = mod, collections, default
        self.session = "/org/freedesktop/secrets/session/1"
        self.log = []
        self.create_prompt = False   # CreateItem needs a prompt
        self.delete_prompt = False   # Delete needs a prompt
        self.corrupt = None          # what get_secrets of copies returns instead
        self.wrong_attributes = False
        self.counter = 0

    secret = lambda self, value, ct="text/plain": (self.session, b"", value, ct)  # noqa: E731

    def collections(self):
        return [p for p in self.cols if p != self.mod.SESSION_COLLECTION]

    def locked(self, path):
        return self.cols[path]["locked"]

    def label(self, path):
        return self.cols[path]["label"]

    def alias(self, name):
        return self.default

    def set_alias(self, name, path):
        self.log.append(("alias", name, path))
        self.default = path

    def create(self, label, key):
        self.log.append(("create", label, key))
        path = BASE + label
        self.cols[path] = {"label": label, "locked": False, "password": key, "items": {}}
        return path

    def unlock(self, path, key):
        self.log.append(("unlock", path))
        if self.cols[path]["password"] != key:
            raise self.mod.GLib.Error("The password was invalid")
        self.cols[path]["locked"] = False

    def items(self, path):
        return list(self.cols[path]["items"])

    def item(self, path):
        for col in self.cols.values():
            if path in col["items"]:
                return col["items"][path]
        raise KeyError(path)

    def get_secrets(self, items):
        out = {}
        for path in items:
            value = self.item(path)["secret"]
            if self.corrupt is not None and path.startswith(BASE + "login/"):
                value = self.corrupt
            # Gio unpacks "ay" as a list of ints
            out[path] = (self.session, [], list(value), self.item(path)["type"])
        return out

    def prop(self, path, interface, name):
        item = self.item(path)
        if name == "Attributes" and self.wrong_attributes and path.startswith(BASE + "login/"):
            return {**item["attributes"], "changed": "1"}
        return item[{"Label": "label", "Attributes": "attributes"}[name]]

    def call(self, path, interface, method, args=None, reply=None, timeout_ms=25000):
        self.log.append((method, path))
        if method == "CreateItem":
            if self.create_prompt:
                return "/", "/org/freedesktop/secrets/prompt/p1"
            properties, secret, replace = args.unpack()
            self.counter += 1
            item = f"{path}/{self.counter}"
            self.cols[path]["items"][item] = {
                "label": properties["org.freedesktop.Secret.Item.Label"],
                "attributes": properties["org.freedesktop.Secret.Item.Attributes"],
                "secret": bytes(secret[2]), "type": secret[3]}
            return item, "/"
        if method == "Delete":
            if self.delete_prompt:
                return ("/org/freedesktop/secrets/prompt/p2",)
            del self.cols[path]
            return ("/",)
        raise AssertionError(method)


def old_keyring(locked=False):
    return {"label": "Default keyring", "locked": locked, "password": b"user-chosen",
            "items": {BASE + "Default_keyring/1": {"label": "GOA", "attributes": {"goa-id": "acc1"},
                                                   "secret": b"dummy-secret-1", "type": "text/plain"},
                      BASE + "Default_keyring/2": {"label": "WiFi", "attributes": {"ssid": "x"},
                                                   "secret": b"\x00\xffbinary", "type": "application/octet-stream"}}}


def login(locked=True, password=DUMMY_KEY):
    return {"label": "login", "locked": locked, "password": password, "items": {}}


@pytest.fixture
def keyring_files(mod, tmp_path, monkeypatch):
    keyrings = tmp_path / "keyrings"
    keyrings.mkdir()
    (keyrings / "Default_keyring.keyring").write_bytes(b"encrypted")
    (keyrings / "login.keyring").write_bytes(b"encrypted too")
    (keyrings / "subdir").mkdir()
    monkeypatch.setattr(mod, "KEYRING_DIR", str(keyrings))
    monkeypatch.setattr(mod, "BACKUP_ROOT", str(tmp_path / "backup"))
    return tmp_path / "backup"


# --- Login keyring: create, unlock, leave alone ------------------------------------------------

def test_open_login_creates_it_when_missing(mod):
    s = FakeSecrets(mod, {mod.SESSION_COLLECTION: {"label": "session", "locked": False, "items": {}}})
    assert mod.open_login(s, DUMMY_KEY) == BASE + "login"
    assert s.log == [("create", "login", DUMMY_KEY)]


def test_open_login_unlocks_with_the_key(mod):
    s = FakeSecrets(mod, {BASE + "login": login()})
    assert mod.open_login(s, DUMMY_KEY) == BASE + "login"
    assert not s.cols[BASE + "login"]["locked"]


def test_open_login_finds_it_by_label(mod):
    s = FakeSecrets(mod, {BASE + "login_1": login(locked=False)})
    assert mod.open_login(s, DUMMY_KEY) == BASE + "login_1"
    assert s.log == []


def test_open_login_never_replaces_a_keyring_with_another_password(mod):
    s = FakeSecrets(mod, {BASE + "login": login(password=b"hand-set password")})
    assert mod.open_login(s, DUMMY_KEY) is None
    assert s.cols[BASE + "login"]["locked"]
    assert [entry[0] for entry in s.log] == ["unlock"]


# --- Moving the old default keyring ----------------------------------------------------------

def test_move_items_copies_checks_backs_up_and_removes(mod, keyring_files):
    old = BASE + "Default_keyring"
    s = FakeSecrets(mod, {old: old_keyring(), BASE + "login": login(locked=False)})
    assert mod.move_items(s, old, BASE + "login") is True
    copied = s.cols[BASE + "login"]["items"].values()
    assert sorted((i["label"], i["secret"], i["type"]) for i in copied) == [
        ("GOA", b"dummy-secret-1", "text/plain"), ("WiFi", b"\x00\xffbinary", "application/octet-stream")]
    assert old not in s.cols
    (backup,) = keyring_files.iterdir()
    assert sorted(p.name for p in backup.iterdir()) == ["Default_keyring.keyring", "login.keyring"]
    assert backup.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("failure", ["create_prompt", "corrupt", "wrong_attributes"])
def test_move_items_keeps_the_old_keyring_on_any_doubt(mod, keyring_files, failure):
    old = BASE + "Default_keyring"
    s = FakeSecrets(mod, {old: old_keyring(), BASE + "login": login(locked=False)})
    setattr(s, failure, b"other" if failure == "corrupt" else True)
    assert mod.move_items(s, old, BASE + "login") is False
    assert old in s.cols and len(s.cols[old]["items"]) == 2
    assert ("Delete", old) not in s.log
    assert not keyring_files.exists()  # nothing to back up: nothing was removed


def test_move_items_delete_needing_a_prompt_keeps_it(mod, keyring_files):
    old = BASE + "Default_keyring"
    s = FakeSecrets(mod, {old: old_keyring(), BASE + "login": login(locked=False)})
    s.delete_prompt = True
    assert mod.move_items(s, old, BASE + "login") is False
    assert old in s.cols
    assert len(list(keyring_files.iterdir())) == 1  # the copy of the files stays


# --- main ------------------------------------------------------------------------------------

@pytest.fixture
def main(mod, key_dir, keyring_files, monkeypatch):
    key_dir.write_bytes(DUMMY_KEY)
    key_dir.chmod(0o600)
    notified = []
    monkeypatch.setattr(mod, "notify", notified.append)
    state = {"secrets": None, "waited": [], "wait_result": True}

    def wait(secrets, collection):
        state["waited"].append(collection)
        if state["wait_result"]:
            secrets.cols[collection]["locked"] = False
        return state["wait_result"]
    monkeypatch.setattr(mod, "wait_until_open", wait)

    def run(collections, default="/"):
        s = state["secrets"] = FakeSecrets(mod, collections, default)

        def factory():
            if isinstance(collections, BaseException):
                raise collections
            return s
        monkeypatch.setattr(mod, "Secrets", factory)
        return mod.main(), s, notified
    state["run"] = run
    return state


def test_main_without_key_still_lets_the_session_start(mod, main, key_dir):
    key_dir.unlink()
    code, s, notified = main["run"]({})
    assert code == 1 and notified == ["READY=1"] and s.log == []


def test_main_without_secret_service(mod, main):
    code, _s, notified = main["run"](mod.GLib.Error("org.freedesktop.DBus.Error.ServiceUnknown"))
    assert code == 1 and notified == ["READY=1"]


def test_main_first_start_creates_login_and_makes_it_default(mod, main):
    code, s, notified = main["run"]({}, default="/")
    assert code == 0 and notified == ["READY=1"]
    assert s.default == BASE + "login"
    assert [e[0] for e in s.log] == ["create", "alias"]


def test_main_wrong_key_changes_nothing(mod, main):
    cols = {BASE + "login": login(password=b"other"), BASE + "Default_keyring": old_keyring()}
    code, s, notified = main["run"](cols, default=BASE + "Default_keyring")
    assert code == 0 and notified == ["READY=1"]
    assert s.default == BASE + "Default_keyring"
    assert BASE + "Default_keyring" in s.cols
    assert [e[0] for e in s.log] == ["unlock"]


def test_main_moves_an_open_old_default_after_ready(mod, main):
    cols = {BASE + "login": login(), BASE + "Default_keyring": old_keyring()}
    code, s, notified = main["run"](cols, default=BASE + "Default_keyring")
    assert code == 0 and notified == ["READY=1"]
    assert s.default == BASE + "login"
    assert BASE + "Default_keyring" not in s.cols
    assert len(s.cols[BASE + "login"]["items"]) == 2
    assert main["waited"] == []


def test_main_waits_for_a_locked_old_default(mod, main):
    cols = {BASE + "login": login(), BASE + "Default_keyring": old_keyring(locked=True)}
    code, s, _ = main["run"](cols, default=BASE + "Default_keyring")
    assert code == 0 and main["waited"] == [BASE + "Default_keyring"]
    assert BASE + "Default_keyring" not in s.cols
    main["waited"].clear()
    main["wait_result"] = False
    cols = {BASE + "login": login(), BASE + "Default_keyring": old_keyring(locked=True)}
    code, s, _ = main["run"](cols, default=BASE + "Default_keyring")
    assert code == 0 and BASE + "Default_keyring" in s.cols
    assert ("Delete", BASE + "Default_keyring") not in s.log


def test_main_never_touches_the_session_collection(mod, main):
    cols = {BASE + "login": login(), mod.SESSION_COLLECTION: {"label": "session", "locked": False,
                                                              "items": {}}}
    code, s, _ = main["run"](cols, default=mod.SESSION_COLLECTION)
    assert code == 0 and mod.SESSION_COLLECTION in s.cols
    assert not any(e[0] in ("Delete", "CreateItem") for e in s.log)


def test_main_failed_backup_keeps_the_old_keyring(mod, main, keyring_files, monkeypatch):
    monkeypatch.setattr(mod, "KEYRING_DIR", str(keyring_files.parent / "no-such-dir"))
    cols = {BASE + "login": login(), BASE + "Default_keyring": old_keyring()}
    code, s, _ = main["run"](cols, default=BASE + "Default_keyring")
    assert code == 1
    assert BASE + "Default_keyring" in s.cols
    assert ("Delete", BASE + "Default_keyring") not in s.log


def test_notify_to_an_abstract_socket(mod, monkeypatch):
    name = f"plinius-test-notify-{os.getpid()}"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind("\0" + name)
        monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)
        mod.notify("READY=1")
        assert sock.recv(64) == b"READY=1"


def test_read_key_that_cannot_be_opened(mod, key_dir, capsys):
    key_dir.write_bytes(DUMMY_KEY)
    key_dir.chmod(0)  # not even readable by its owner
    try:
        if os.access(key_dir, os.R_OK):
            pytest.skip("running with privileges that ignore modes")
        assert mod.read_key() is None
    finally:
        key_dir.chmod(0o600)
    assert "cannot open device key" in capsys.readouterr().err


# --- The Secrets wrapper against a fake session bus ------------------------------------------

class FakeBus:
    """call_sync answers from replies {method: (type, value)}; every call is recorded with
    its argument type and value, the reply type and the timeout."""

    def __init__(self, mod, replies):
        self.mod, self.replies = mod, dict(replies)
        self.calls = []
        self.subscriptions = []

    def call_sync(self, name, path, iface, method, args, reply_type, flags, timeout, cancel):
        assert name == self.mod.BUS_NAME and cancel is None
        key = method if method != "Get" else ("Get", path, args.unpack()[1])
        self.calls.append((path, iface, method,
                           args.get_type_string() if args is not None else None,
                           args.unpack() if args is not None else None,
                           reply_type.dup_string() if reply_type is not None else None,
                           timeout))
        answer = self.replies[key]
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            return None
        if callable(answer):
            answer = answer()
        return self.mod.GLib.Variant(*answer)

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, callback):
        self.subscriptions.append((sender, iface, member, path, callback))
        return len(self.subscriptions)


SESSION = "/org/freedesktop/secrets/session/s1"


@pytest.fixture
def bus_secrets(mod, monkeypatch):
    def make(replies):
        bus = FakeBus(mod, {"OpenSession": ("(vo)", (mod.GLib.Variant("s", ""), SESSION)),
                            **replies})
        monkeypatch.setattr(mod.Gio, "bus_get_sync", lambda kind, cancel: bus)
        secrets = mod.Secrets()
        return secrets, bus
    return make


def test_secrets_opens_a_plain_session(mod, bus_secrets):
    secrets, bus = bus_secrets({})
    assert secrets.session == SESSION and secrets.bus is bus
    (path, iface, method, sig, args, reply, timeout), = bus.calls
    assert (path, iface, method) == (mod.SERVICE_PATH, mod.SERVICE, "OpenSession")
    assert sig == "(sv)" and args == ("plain", "") and reply == "(vo)" and timeout == 25000


def test_secrets_properties_and_aliases(mod, bus_secrets):
    login_path = BASE + "login"
    secrets, bus = bus_secrets({
        ("Get", mod.SERVICE_PATH, "Collections"): ("(v)", (mod.GLib.Variant(
            "ao", [mod.SESSION_COLLECTION, login_path, BASE + "other"]),)),
        ("Get", login_path, "Locked"): ("(v)", (mod.GLib.Variant("b", True),)),
        ("Get", login_path, "Label"): ("(v)", (mod.GLib.Variant("s", "login"),)),
        ("Get", login_path, "Items"): ("(v)", (mod.GLib.Variant("ao", [login_path + "/1"]),)),
        "ReadAlias": ("(o)", (login_path,)),
        "SetAlias": None,
    })
    # The session collection (unlocked in memory, never a keyring) is no candidate
    assert secrets.collections() == [login_path, BASE + "other"]
    assert secrets.locked(login_path) is True
    assert secrets.label(login_path) == "login"
    assert secrets.items(login_path) == [login_path + "/1"]
    assert secrets.alias("default") == login_path
    assert secrets.set_alias("default", login_path) is None
    by_method = {c[2]: c for c in bus.calls}
    assert by_method["Get"][1] == mod.PROPERTIES and by_method["Get"][3] == "(ss)"
    assert bus.calls[2][4] == (mod.COLLECTION, "Locked")
    assert by_method["ReadAlias"][3:6] == ("(s)", ("default",), "(o)")
    assert by_method["SetAlias"][3:6] == ("(so)", ("default", login_path), None)


def test_secrets_create_and_unlock_with_the_master_password(mod, bus_secrets):
    secrets, bus = bus_secrets({"CreateWithMasterPassword": ("(o)", (BASE + "login",)),
                                "UnlockWithMasterPassword": None})
    assert secrets.create("login", DUMMY_KEY) == BASE + "login"
    secrets.unlock(BASE + "login", DUMMY_KEY)
    create, unlock = bus.calls[1:]
    assert create[1:4] == (mod.INTERNAL, "CreateWithMasterPassword", "(a{sv}(oayays))")
    properties, secret = create[4]
    assert properties == {"org.freedesktop.Secret.Collection.Label": "login"}
    # Gio unpacks "ay" as a list of ints
    assert secret == (SESSION, [], list(DUMMY_KEY), "text/plain")
    assert unlock[1:4] == (mod.INTERNAL, "UnlockWithMasterPassword", "(o(oayays))")
    assert unlock[4] == (BASE + "login", (SESSION, [], list(DUMMY_KEY), "text/plain"))


def test_secrets_get_secrets(mod, bus_secrets):
    item = BASE + "login/1"
    secrets, bus = bus_secrets({"GetSecrets": ("(a{o(oayays)})", (
        {item: (SESSION, b"", b"dummy", "text/plain")},))})
    assert secrets.get_secrets([]) == {} and len(bus.calls) == 1  # no call for nothing
    assert secrets.get_secrets([item]) == {item: (SESSION, [], list(b"dummy"), "text/plain")}
    assert bus.calls[1][3:5] == ("(aoo)", ([item], SESSION))


def test_find_login_skips_other_keyrings(mod):
    s = FakeSecrets(mod, {BASE + "Default_keyring": old_keyring(),
                          BASE + "login_2": login(locked=False)})
    assert mod.find_login(s) == BASE + "login_2"
    assert mod.find_login(FakeSecrets(mod, {BASE + "Default_keyring": old_keyring()})) is None


# --- Waiting for the user to unlock the old keyring once -------------------------------------

class WaitingSecrets(FakeSecrets):
    """FakeSecrets with a bus that records signal subscriptions (main loop is GLib's own)."""

    def __init__(self, mod, collections):
        super().__init__(mod, collections)
        self.bus = FakeBus(mod, {})
        self.fail = False

    def collections(self):
        if self.fail:
            raise self.mod.GLib.Error("Secret Service gone")
        return super().collections()


def emit_later(mod, secrets, change):
    """After the loop started: change the keyring, then gnome-keyring's signal arrives."""
    def fire():
        change()
        for _sender, _iface, _member, _path, callback in secrets.bus.subscriptions[:1]:
            callback(None, ":1.5", mod.SERVICE_PATH, mod.SERVICE, "CollectionChanged", None)
        return False
    mod.GLib.timeout_add(10, fire)
    # A test never hangs: the loop is ended after 5 s whatever happens
    guard = mod.GLib.timeout_add_seconds(5, lambda: pytest.fail("loop did not end"))
    return guard


def test_wait_until_open_returns_at_once_for_an_open_keyring(mod):
    old = BASE + "Default_keyring"
    s = WaitingSecrets(mod, {old: old_keyring(locked=False)})
    assert mod.wait_until_open(s, old) is True
    assert [sub[2] for sub in s.bus.subscriptions] == ["CollectionChanged", "CollectionDeleted"]
    assert all(sub[:2] == (mod.BUS_NAME, mod.SERVICE) for sub in s.bus.subscriptions)


def test_wait_until_open_waits_for_the_unlock(mod):
    old = BASE + "Default_keyring"
    s = WaitingSecrets(mod, {old: old_keyring(locked=True)})
    guard = emit_later(mod, s, lambda: s.cols[old].update(locked=False))
    try:
        assert mod.wait_until_open(s, old) is True
    finally:
        mod.GLib.source_remove(guard)


def test_wait_until_open_ends_when_the_keyring_is_deleted(mod):
    old = BASE + "Default_keyring"
    s = WaitingSecrets(mod, {old: old_keyring(locked=True)})
    guard = emit_later(mod, s, lambda: s.cols.pop(old))
    try:
        assert mod.wait_until_open(s, old) is False
    finally:
        mod.GLib.source_remove(guard)


def test_wait_until_open_ends_when_the_service_goes_away(mod, capsys):
    old = BASE + "Default_keyring"
    s = WaitingSecrets(mod, {old: old_keyring(locked=True)})

    def gone():
        s.fail = True
    guard = emit_later(mod, s, gone)
    try:
        assert mod.wait_until_open(s, old) is False
    finally:
        mod.GLib.source_remove(guard)
    assert "keyring went away while waiting: Secret Service gone" in capsys.readouterr().err


def test_wait_until_open_for_a_keyring_already_gone(mod):
    s = WaitingSecrets(mod, {})
    assert mod.wait_until_open(s, BASE + "Default_keyring") is False


def test_main_with_login_already_the_default(mod, main):
    cols = {BASE + "login": login()}
    code, s, notified = main["run"](cols, default=BASE + "login")
    assert code == 0 and notified == ["READY=1"]
    assert not any(e[0] == "alias" for e in s.log)


def test_as_a_program_without_a_key(tmp_path, monkeypatch, capsys):
    import runpy
    monkeypatch.setenv("PLINIUS_LOGIN_KEYRING_KEY_DIR", str(tmp_path))
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    with pytest.raises(SystemExit) as exit_:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exit_.value.code == 1
    assert f"no device key at {tmp_path}/{os.getuid()}" in capsys.readouterr().err
