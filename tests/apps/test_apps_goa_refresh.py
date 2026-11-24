# plinius-goa-refresh (pkgbuilds/gnome-online-accounts-plinius): when the password of an online
# account is handed to evolution-data-server, how often (a rejecting server must not be
# guessed against), retries, waiting for the keyring, and the refresh of one account.
#
# EDS, GOA and the keyring are fakes: the gi modules EDataServer, EBook, ECal and Goa are
# replaced while the script is imported, keyring_locked is replaced (it would ask the session
# bus), time is a fake clock. Passwords here are dummies.

import sys
import types

import pytest

from conftest import PKG, load_script

SCRIPT = PKG / "gnome-online-accounts-plinius" / "plinius-goa-refresh"


class Reason:
    def __init__(self, nick):
        self.value_nick = nick

    def __repr__(self):
        return self.value_nick


REASONS = types.SimpleNamespace(**{n.upper().replace("-", "_"): Reason(n) for n in
                                   ("unknown", "required", "rejected", "ssl-failed", "error")})


class NamedParameters(dict):
    @classmethod
    def new(cls):
        return cls()

    def set(self, key, value):
        self[key] = value


def fake_gi(GLib, Gio):
    eds = types.SimpleNamespace(
        SOURCE_EXTENSION_ADDRESS_BOOK="Address Book", SOURCE_EXTENSION_CALENDAR="Calendar",
        SOURCE_EXTENSION_TASK_LIST="Task List", SOURCE_EXTENSION_MEMO_LIST="Memo List",
        SOURCE_EXTENSION_GOA="GNOME Online Accounts", SOURCE_EXTENSION_COLLECTION="Collection",
        SOURCE_CREDENTIAL_PASSWORD="password", SourceCredentialsReason=REASONS,
        NamedParameters=NamedParameters, SourceRegistry=None)
    ecal = types.SimpleNamespace(ClientSourceType=types.SimpleNamespace(EVENTS="events", TASKS="tasks",
                                                                        MEMOS="memos"), Client=None)
    repository = types.ModuleType("gi.repository")
    repository.__dict__.update(EDataServer=eds, ECal=ecal, EBook=types.SimpleNamespace(BookClient=None),
                               Goa=types.SimpleNamespace(Client=None), GLib=GLib, Gio=Gio)
    gi = types.ModuleType("gi")
    gi.require_version = lambda name, version: None
    gi.repository = repository
    return gi, repository


@pytest.fixture(scope="module")
def mod():
    pytest.importorskip("gi")
    from gi.repository import Gio, GLib
    gi, repository = fake_gi(GLib, Gio)
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "gi", gi)
        mp.setitem(sys.modules, "gi.repository", repository)
        return load_script(SCRIPT)


class Clock:
    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        for hook in list(self.hooks):
            hook(self.now)

    hooks = ()


class Extension:
    def __init__(self, account_id):
        self.account_id = account_id

    def get_account_id(self):
        return self.account_id


class Source:
    """An EDS source. reason/error are what EDS reports for the last attempt; `server` decides
    what a given password leads to."""

    def __init__(self, mod, uid, extensions, account_id="acc1", parent=None, enabled=True,
                 reason=None, error=None, server=None):
        self.mod, self.uid, self.extensions = mod, uid, set(extensions)
        self.account_id, self.parent, self.enabled = account_id, parent, enabled
        self.reason, self.error = reason or REASONS.UNKNOWN, error
        self.server = server or (lambda password: (REASONS.UNKNOWN, None))
        self.passwords = []

    def has_extension(self, ext):
        return ext in self.extensions

    def get_extension(self, ext):
        return Extension(self.account_id)

    def get_uid(self):
        return self.uid

    def get_parent(self):
        return self.parent

    def get_enabled(self):
        return self.enabled

    def get_display_name(self):
        return self.uid

    def get_last_credentials_required_arguments_sync(self, cancellable):
        if isinstance(self.reason, BaseException):
            raise self.reason
        err = self.mod.GLib.Error(self.error) if self.error else None
        return True, self.reason, None, 0, err

    def invoke_authenticate_sync(self, credentials, cancellable):
        self.passwords.append(credentials["password"])
        self.reason, self.error = self.server(credentials["password"])


class Registry:
    def __init__(self, sources, on_refresh=None):
        self.sources, self.refreshed = sources, []
        self.on_refresh = on_refresh

    def list_sources(self, ext):
        return [s for s in self.sources if ext is None or s.has_extension(ext)]

    def refresh_backend_sync(self, uid, cancellable):
        self.refreshed.append(uid)
        if self.on_refresh:
            self.on_refresh(uid)


class Account:
    def __init__(self, account_id, password):
        self.id, self.password = account_id, password

    def get_account(self):
        return types.SimpleNamespace(props=types.SimpleNamespace(id=self.id))

    def get_password_based(self):
        if self.password is None:
            return None  # OAuth
        return types.SimpleNamespace(call_get_password_sync=lambda key, c: self.password)


@pytest.fixture
def eds(mod, monkeypatch):
    clock = Clock()
    clock.hooks = []
    monkeypatch.setattr(mod, "time", clock)
    accounts = [Account("acc1", "dummy-password"), Account("oauth", None)]
    monkeypatch.setattr(mod.Goa, "Client", types.SimpleNamespace(
        new_sync=lambda c: types.SimpleNamespace(get_accounts=lambda: accounts)))
    locked = {"value": False}
    monkeypatch.setattr(mod, "keyring_locked", lambda: locked["value"])
    GOA, COLL = mod.EDataServer.SOURCE_EXTENSION_GOA, mod.EDataServer.SOURCE_EXTENSION_COLLECTION

    def collection(uid="coll", **kw):
        return Source(mod, uid, {GOA, COLL}, **kw)
    return types.SimpleNamespace(clock=clock, locked=locked, collection=collection, accounts=accounts)


def accepts(password):
    return (REASONS.UNKNOWN, None) if password == "dummy-password" else (REASONS.REJECTED, "denied")


def test_missing_password_is_given_once_and_lookup_repeated(mod, eds):
    source = eds.collection(reason=REASONS.REQUIRED, server=accepts)
    registry = Registry([source])
    assert mod.discover(registry, source) is None
    assert source.passwords == ["dummy-password"]
    assert registry.refreshed == ["coll"]


def test_rejecting_server_gets_the_password_at_most_twice(mod, eds):
    source = eds.collection(reason=REASONS.REJECTED, error="denied",
                            server=lambda pw: (REASONS.REJECTED, "denied"))
    registry = Registry([source])
    assert mod.discover_all(registry) is False
    # No retries with the same password: they would count as guessing on the server
    assert len(source.passwords) == 2
    assert eds.clock.sleeps.count(mod.DISCOVER_RETRY_SECONDS) == 0


def test_password_asked_only_after_the_lookup(mod, eds):
    source = eds.collection(server=accepts)
    registry = Registry([source], on_refresh=lambda uid: setattr(source, "reason", REASONS.REQUIRED)
                        if not source.passwords else None)
    assert mod.discover(registry, source) is None
    assert source.passwords == ["dummy-password"]
    assert registry.refreshed == ["coll", "coll"]


def test_network_errors_are_retried(mod, eds):
    source = eds.collection(reason=REASONS.ERROR, error="Cannot resolve hostname")
    attempts = []

    def refresh(uid):
        attempts.append(uid)
        if len(attempts) == 2:
            source.reason, source.error = REASONS.UNKNOWN, None
    assert mod.discover_all(Registry([source], on_refresh=refresh)) is True
    assert len(attempts) == 2
    assert eds.clock.sleeps.count(mod.DISCOVER_RETRY_SECONDS) == 1
    assert source.passwords == []


def test_network_error_that_stays_fails_after_all_attempts(mod, eds):
    source = eds.collection(reason=REASONS.ERROR, error="Cannot resolve hostname")
    registry = Registry([source])
    assert mod.discover_all(registry) is False
    assert len(registry.refreshed) == mod.DISCOVER_ATTEMPTS


def test_locked_keyring_or_oauth_gives_no_password(mod, eds):
    source = eds.collection(reason=REASONS.REQUIRED)
    eds.locked["value"] = True
    assert mod.give_password(source) is False
    eds.locked["value"] = False
    oauth = eds.collection(account_id="oauth", reason=REASONS.REQUIRED)
    assert mod.give_password(oauth) is False
    unknown = eds.collection(account_id="nobody", reason=REASONS.REQUIRED)
    assert mod.give_password(unknown) is False
    assert source.passwords == oauth.passwords == unknown.passwords == []


def test_authenticate_failure_is_reported_as_not_given(mod, eds):
    source = eds.collection(reason=REASONS.REQUIRED)

    def fail(credentials, cancellable):
        raise mod.GLib.Error("backend gone")
    source.invoke_authenticate_sync = fail
    assert mod.give_password(source) is False


def test_last_error_and_credentials_missing(mod, eds):
    s = eds.collection()
    assert mod.last_error(s) is None and not mod.credentials_missing(s)
    s.reason, s.error = REASONS.SSL_FAILED, None
    assert mod.last_error(s) == "ssl-failed" and not mod.credentials_missing(s)
    s.reason, s.error = REASONS.REJECTED, "Unauthorized"
    assert mod.last_error(s) == "Unauthorized" and mod.credentials_missing(s)
    s.reason = mod.GLib.Error("no such source")
    assert mod.last_error(s) == "no such source" and not mod.credentials_missing(s)


def test_lookup_waits_until_nothing_new_appears(mod, eds):
    source = eds.collection()
    registry = Registry([source])
    start = eds.clock.now

    def appear(now):
        # Children appear during the first two seconds
        if now - start in (1.0, 2.0):
            registry.sources.append(Source(mod, f"child{now}", {"Calendar"}, parent="coll"))
    eds.clock.hooks.append(appear)
    mod.lookup(registry, source)
    assert len(mod.children(registry, source)) == 2
    assert eds.clock.now - start == pytest.approx(2.0 + mod.SETTLE_SECONDS)


def test_lookup_with_a_lasting_error_ends_at_the_limit(mod, eds):
    source = eds.collection(reason=REASONS.ERROR, error="timeout")
    start = eds.clock.now
    mod.lookup(Registry([source]), source)
    assert eds.clock.now - start == pytest.approx(mod.SETTLE_LIMIT)


def test_account_sources_filters_by_account_and_collection(mod, eds):
    a, b = eds.collection("a"), eds.collection("b", account_id="acc2")
    plain = Source(mod, "c", {mod.EDataServer.SOURCE_EXTENSION_GOA})
    registry = Registry([a, b, plain])
    assert mod.account_sources(registry) == [a, b]
    assert mod.account_sources(registry, "acc2") == [b]


class Client:
    def __init__(self, refresh_supported=True, fail=None):
        self.refresh_supported, self.fail, self.refreshed = refresh_supported, fail, 0

    def check_refresh_supported(self):
        return self.refresh_supported

    def refresh_sync(self, cancellable):
        if self.fail:
            raise self.fail
        self.refreshed += 1


def test_refresh_account_fetches_enabled_children(mod, eds, monkeypatch):
    connected = []
    book, cal = Client(), Client(refresh_supported=False)

    def book_connect(source, wait, c):
        connected.append(("book", source.uid, wait))
        return book

    def cal_connect(source, kind, wait, c):
        connected.append((kind, source.uid, wait))
        return cal
    monkeypatch.setattr(mod.EBook, "BookClient", types.SimpleNamespace(connect_sync=book_connect))
    monkeypatch.setattr(mod.ECal, "Client", types.SimpleNamespace(connect_sync=cal_connect))
    coll = eds.collection()
    registry = Registry([coll,
                         Source(mod, "contacts", {"Address Book"}, parent="coll"),
                         Source(mod, "cal", {"Calendar", "Task List"}, parent="coll"),
                         Source(mod, "off", {"Calendar"}, parent="coll", enabled=False),
                         Source(mod, "foreign", {"Calendar"}, parent="other")])
    assert mod.refresh_account(registry, "acc1") is True
    assert connected == [("book", "contacts", mod.NO_WAIT), ("events", "cal", mod.NO_WAIT),
                         ("tasks", "cal", mod.NO_WAIT)]
    assert book.refreshed == 1 and cal.refreshed == 0
    # The backends work while their clients are open
    assert eds.clock.sleeps[-1] == mod.FETCH_SECONDS


def test_refresh_account_reports_fetch_errors(mod, eds, monkeypatch):
    monkeypatch.setattr(mod.ECal, "Client", types.SimpleNamespace(
        connect_sync=lambda *a: Client(fail=mod.GLib.Error("HTTP 500"))))
    registry = Registry([eds.collection(), Source(mod, "cal", {"Calendar"}, parent="coll")])
    assert mod.refresh_account(registry, "acc1") is False
    assert mod.refresh_account(registry, "no-such-account") is True


def test_wait_for_keyring(mod, eds, monkeypatch):
    polls = iter([True, True, True, False])
    monkeypatch.setattr(mod, "keyring_locked", lambda: next(polls))
    mod.wait_for_keyring()
    assert eds.clock.sleeps == [mod.KEYRING_POLL_SECONDS] * 3
    eds.clock.sleeps.clear()
    monkeypatch.setattr(mod, "keyring_locked", lambda: True)
    mod.wait_for_keyring()
    assert sum(eds.clock.sleeps) == pytest.approx(mod.KEYRING_WAIT_SECONDS)


@pytest.mark.parametrize("argv, code", [([], 2), (["a", "b"], 2), (["-x"], 2), (["--help"], 2)])
def test_main_usage(mod, monkeypatch, argv, code):
    monkeypatch.setattr(sys, "argv", ["plinius-goa-refresh", *argv])
    monkeypatch.setattr(mod.threading, "Timer", lambda *a: pytest.fail("no timer for a usage error"))
    assert mod.main() == code


@pytest.mark.parametrize("arg, ok, code", [("--discover", True, 0), ("--discover", False, 1),
                                            ("acc1", True, 0), ("acc1", False, 1)])
def test_main_dispatch(mod, eds, monkeypatch, arg, ok, code):
    calls = []

    class Timer:
        def __init__(self, seconds, function):
            calls.append(("timer", seconds))
            self.daemon = False

        def start(self):
            assert self.daemon  # must not keep the process alive

    monkeypatch.setattr(mod.threading, "Timer", Timer)
    monkeypatch.setattr(mod.EDataServer, "SourceRegistry",
                        types.SimpleNamespace(new_sync=lambda c: "registry"))
    monkeypatch.setattr(mod, "wait_for_keyring", lambda: calls.append("keyring"))
    monkeypatch.setattr(mod, "discover_all", lambda r: calls.append(("all", r)) or ok)
    monkeypatch.setattr(mod, "refresh_account", lambda r, a: calls.append(("one", r, a)) or ok)
    monkeypatch.setattr(sys, "argv", ["plinius-goa-refresh", arg])
    assert mod.main() == code
    if arg == "--discover":
        # The keyring wait comes before the time limit starts
        assert calls == ["keyring", ("timer", mod.TIME_LIMIT), ("all", "registry")]
    else:
        assert calls == [("timer", mod.TIME_LIMIT), ("one", "registry", "acc1")]


# --- The keyring's state on the session bus, errors on the way, the program -------------------

class SecretsBus:
    def __init__(self, mod, locked):
        self.mod, self.locked, self.calls = mod, locked, []

    def call_sync(self, name, path, iface, method, params, reply_type, flags, timeout, cancel):
        self.calls.append((name, path, iface, method, params.unpack(), reply_type.dup_string()))
        if isinstance(self.locked, BaseException):
            raise self.locked
        V = self.mod.GLib.Variant
        return V("(v)", (V("b", self.locked),))


@pytest.mark.parametrize("answer, locked", [(True, True), (False, False), ("error", False)])
def test_keyring_locked_asks_the_default_collection(mod, monkeypatch, answer, locked):
    bus = SecretsBus(mod, mod.GLib.Error("no secret service") if answer == "error" else answer)
    kinds = []
    gio = types.SimpleNamespace(BusType=mod.Gio.BusType, DBusCallFlags=mod.Gio.DBusCallFlags,
                                bus_get_sync=lambda kind, c: kinds.append(kind) or bus)
    monkeypatch.setattr(mod, "Gio", gio)
    # An unknown state counts as open: GOA then reports the problem itself
    assert mod.keyring_locked() is locked
    assert kinds == [mod.Gio.BusType.SESSION]
    assert bus.calls == [("org.freedesktop.secrets", "/org/freedesktop/secrets/aliases/default",
                          "org.freedesktop.DBus.Properties", "Get",
                          ("org.freedesktop.Secret.Collection", "Locked"), "(v)")]


def test_account_password_when_goa_is_unavailable(mod, eds, monkeypatch, capsys):
    def unavailable(cancellable):
        raise mod.GLib.Error("goa-daemon not running")
    monkeypatch.setattr(mod.Goa, "Client", types.SimpleNamespace(new_sync=unavailable))
    assert mod.account_password("acc1") is None
    assert "Could not read the password: goa-daemon not running" in capsys.readouterr().err


def test_refresh_account_reports_a_lookup_error_and_still_fetches(mod, eds, monkeypatch, capsys):
    book = Client()
    monkeypatch.setattr(mod.EBook, "BookClient", types.SimpleNamespace(
        connect_sync=lambda source, wait, c: book))
    coll = eds.collection(reason=REASONS.ERROR, error="Cannot resolve hostname")
    registry = Registry([coll, Source(mod, "contacts", {"Address Book"}, parent="coll")])
    assert mod.refresh_account(registry, "acc1") is False
    assert "coll: Cannot resolve hostname" in capsys.readouterr().err
    # The address book known from before is fetched anyway
    assert book.refreshed == 1 and eds.clock.sleeps[-1] == mod.FETCH_SECONDS


def test_refresh_account_without_calendars_does_not_wait(mod, eds):
    registry = Registry([eds.collection()])
    start = eds.clock.now
    assert mod.refresh_account(registry, "acc1") is True
    assert mod.FETCH_SECONDS not in eds.clock.sleeps
    # Only the lookup's settling time passed
    assert eds.clock.now - start == pytest.approx(mod.SETTLE_SECONDS)


def test_as_a_program_shows_the_usage(monkeypatch, capsys):
    import runpy
    from gi.repository import Gio, GLib
    gi, repository = fake_gi(GLib, Gio)
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    monkeypatch.setattr(sys, "argv", ["plinius-goa-refresh"])
    with pytest.raises(SystemExit) as exit_:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exit_.value.code == 2
    assert "plinius-goa-refresh --discover" in capsys.readouterr().err
