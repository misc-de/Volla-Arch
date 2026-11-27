# plinius-repo-stamp (pkgbuilds/plinius-software): the freshness stamp of [plinius] checked
# against the database pacman fetched: signature (trusted, not revoked), channel, replay against
# the user's and the system-wide state, age, the database and signature hashes the stamp names,
# the grace period before HASH_REQUIRED_FROM, the answers of the server, and the exit codes that
# plinius-pkg and plinius-software act on.
#
# Nothing reaches the system: pacman.conf, the keyrings and the state paths point into tmp_path,
# urlopen and sleep are replaced, signatures use throw-away keys, "root" is a patched geteuid.

import hashlib
import io
import json
import os
import runpy
import subprocess
import sys
import time
import types
import urllib.error

import pytest

from apps_gpgkeys import TestKeys, gpg_keys  # noqa: F401  (fixture)
from conftest import PKG, load_script

SCRIPT = PKG / "plinius-software" / "plinius-repo-stamp"
STABLE = "https://plinius.example/repo/$arch"
BETA = "https://plinius.example/repo/beta/aarch64"


@pytest.fixture(scope="module")
def mod():
    return load_script(SCRIPT)


class Response(io.BytesIO):
    pass


class Clock:
    """time for the module under test: the real time unless `now` is set; sleep only records."""

    def __init__(self):
        self.now, self.slept = None, []

    def time(self):
        return time.time() if self.now is None else self.now

    def sleep(self, seconds):
        self.slept.append(seconds)


@pytest.fixture
def stamp(mod, gpg_keys, tmp_path, monkeypatch, capsys):
    """A [plinius] server with a signed stamp naming the local database; returns helpers."""
    keys, trusted, attacker = gpg_keys
    keyring = tmp_path / "keyrings"
    keyring.mkdir()
    (keyring / "plinius.gpg").write_bytes(keys.export(trusted))
    (keyring / "plinius-trusted").write_text(f"{trusted}:4:\n")
    (keyring / "plinius-revoked").write_text("")
    conf = tmp_path / "pacman.conf"
    conf.write_text(f"[options]\nArchitecture = auto\n[plinius]\nServer = {STABLE}\n")
    monkeypatch.setattr(mod, "PACMAN_CONF", str(conf))
    monkeypatch.setattr(mod, "KEYRING", str(keyring / "plinius.gpg"))
    monkeypatch.setattr(mod, "TRUSTED", str(keyring / "plinius-trusted"))
    monkeypatch.setattr(mod, "REVOKED", str(keyring / "plinius-revoked"))
    monkeypatch.setattr(mod, "SYSTEM_STATE", str(tmp_path / "var" / "repo-stamp"))
    # Not tied to the calendar: the tests of the deadline set it themselves
    monkeypatch.setattr(mod, "HASH_REQUIRED_FROM", int(time.time()) + 10 * 365 * 86400)
    clock = Clock()
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(time=clock.time, sleep=clock.sleep))
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)

    sync = tmp_path / "sync"
    sync.mkdir()
    db = sync / "plinius.db"
    db.write_bytes(b"the database pacman fetched")
    (sync / "plinius.db.sig").write_bytes(b"its signature")
    server = {"files": {}, "calls": []}

    def urlopen(req, timeout=None):
        assert timeout == mod.TIMEOUT
        assert req.get_header("User-agent") == "Plinius Arch Linux"
        url = req.full_url
        server["calls"].append(url)
        answer = server["files"].get(url.rsplit("/", 1)[-1])
        if callable(answer):
            answer = answer()
        if answer is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if isinstance(answer, BaseException):
            raise answer
        return Response(answer)
    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    class Stamp:
        @staticmethod
        def use_server(url):
            conf.write_text(f"[plinius]\nServer = {url}\n")

        @staticmethod
        def serve(channel="stable", when=None, by=trusted, db_hash=True, sig_hash=True, text=None):
            """Sign and serve a stamp; db_hash/sig_hash True: of the local files, None: left out."""
            if text is None:
                text = f"channel={channel}\ntime={int(time.time()) - 3600 if when is None else when}\n"
                if db_hash is not None:
                    text += f"db=plinius.db.tar.gz\ndb_sha256={sha(db) if db_hash is True else db_hash}\n"
                if sig_hash is not None:
                    text += f"sig_sha256={sha(sync / 'plinius.db.sig') if sig_hash is True else sig_hash}\n"
            data = text.encode()
            server["files"]["plinius.stamp"] = data
            server["files"]["plinius.stamp.sig"] = keys.sign(by, data)
            return data

        @staticmethod
        def check(*extra, user_state=True, text=False):
            """main() as plinius-software (user state) or plinius-pkg (--text) calls it:
            (exit code, verdict or the text line)."""
            argv = ["plinius-repo-stamp", "check", "--db", str(db)]
            if user_state:
                argv += ["--user-state", str(Stamp.user)]
            argv += list(extra) + (["--text"] if text else [])
            rc = mod.main(argv)
            out = capsys.readouterr()
            Stamp.err = out.err
            return rc, (out.out if text else json.loads(out.out))

        @staticmethod
        def seen(channel="stable", system=False):
            path = f"{mod.SYSTEM_STATE if system else Stamp.user}-{channel}"
            with open(path, encoding="utf-8") as f:
                return json.load(f)

    Stamp.state, Stamp.user, Stamp.db, Stamp.keyrings, Stamp.clock = (
        server, tmp_path / "user" / "repo-stamp", db, keyring, clock)
    Stamp.trusted_fpr, Stamp.attacker_fpr = trusted, attacker
    Stamp.export = staticmethod(keys.export)
    return Stamp


# --- Server of [plinius] -------------------------------------------------------------------------

def test_plinius_server(mod, tmp_path, monkeypatch):
    conf = tmp_path / "pacman.conf"
    monkeypatch.setattr(mod, "PACMAN_CONF", str(conf))
    assert mod.plinius_server() is None  # no file
    conf.write_text("[options]\nArchitecture = auto\n[core]\nServer = https://other/core\n"
                    "[plinius]\nSigLevel = Required\nServer=https://plinius.example/repo/beta/$arch\n"
                    "Server = https://second/\n")
    assert mod.plinius_server() == "https://plinius.example/repo/beta/$arch"
    conf.write_text("[core]\nServer = https://other/core\n[plinius]\nSigLevel = Never\n")
    assert mod.plinius_server() is None


def test_no_check_without_https_server(stamp, tmp_path):
    (tmp_path / "pacman.conf").write_text("[core]\nServer = https://mirror.example/core\n")
    assert stamp.check()[1]["result"] == "skip"
    for url in ("file:///srv/repo/$arch", "http://plinius.example/repo/$arch"):
        stamp.use_server(url)
        assert stamp.check() == (0, {"result": "skip",
                                     "message": "no https server for [plinius], nothing to check"})
    assert stamp.state["calls"] == []


# --- A good stamp --------------------------------------------------------------------------------

def test_fresh_stamp_matching_the_database_passes_and_is_remembered(stamp, mod):
    when = int(time.time()) - 3600
    stamp.serve(when=when)
    rc, verdict = stamp.check()
    assert rc == 0
    assert verdict == {"result": "ok", "channel": "stable", "time": when, "days": 0,
                       "message": "the stamp of stable matches the database"}
    assert stamp.state["calls"] == [STABLE + "/plinius.stamp", STABLE + "/plinius.stamp.sig"]
    seen = stamp.seen()
    assert seen["time"] == when and seen["hashed"] is True and abs(seen["checked"] - time.time()) < 60
    # As a user, the system-wide state is only read
    assert not os.path.exists(f"{mod.SYSTEM_STATE}-stable")


def test_as_root_the_state_is_kept_system_wide(stamp, mod, monkeypatch):
    when = int(time.time()) - 60
    stamp.serve(when=when)
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    assert stamp.check(user_state=False, text=True) == (
        0, "plinius-repo-stamp: the stamp of stable matches the database\n")
    assert stamp.seen(system=True)["time"] == when
    assert os.stat(f"{mod.SYSTEM_STATE}-stable").st_mode & 0o777 == 0o644
    assert not stamp.user.parent.exists()
    # Root never writes the user's file, even when named
    stamp.serve(when=when + 1)
    stamp.check()
    assert stamp.seen(system=True)["time"] == when + 1 and not stamp.user.parent.exists()


def test_a_user_without_user_state_writes_nothing(stamp, mod):
    stamp.serve()
    assert stamp.check(user_state=False)[0] == 0
    assert not os.path.exists(f"{mod.SYSTEM_STATE}-stable") and not stamp.user.parent.exists()


def test_beta_server_wants_the_beta_stamp(stamp):
    stamp.use_server(BETA)
    stamp.serve(channel="beta")
    assert stamp.check()[1]["result"] == "ok"
    assert stamp.seen("beta")["time"]
    # beta's database handed out under stable, and the other way round
    stamp.serve(channel="stable")
    rc, verdict = stamp.check()
    assert rc == 1 and verdict["result"] == "channel"
    assert verdict["message"] == "the stamp is of channel stable, not beta"
    stamp.use_server(STABLE)
    stamp.serve(channel="beta")
    assert stamp.check()[1]["message"] == "the stamp is of channel beta, not stable"
    stamp.serve(text="time=1\n")  # no channel at all
    assert stamp.check()[1]["result"] == "channel"
    assert not os.path.exists(f"{stamp.user}-stable")


# --- Replay ---------------------------------------------------------------------------------------

def test_older_stamp_than_seen_is_a_replay(stamp):
    now = int(time.time())
    stamp.serve(when=now - 100)
    assert stamp.check()[0] == 0
    stamp.serve(when=now - 100)  # the same stamp again is no replay
    assert stamp.check()[0] == 0
    stamp.serve(when=now - 200)
    rc, verdict = stamp.check()
    assert (rc, verdict["result"]) == (1, "replay")
    assert verdict["message"] == "the stamp is older than one seen before"
    assert stamp.seen()["time"] == now - 100  # the newest seen stays


def test_replay_found_from_the_system_wide_state(stamp, mod, monkeypatch):
    # Root saw a newer stamp (plinius-pkg upgrade); a new account starts from there
    now = int(time.time())
    stamp.serve(when=now - 60)
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    assert stamp.check(user_state=False)[0] == 0
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    stamp.serve(when=now - 3600)
    assert stamp.check()[1]["result"] == "replay"


def test_old_user_state_of_a_bare_time_still_counts(stamp):
    # Up to 0.12-22 plinius-software kept only the time
    now = int(time.time())
    stamp.user.parent.mkdir()
    (stamp.user.parent / "repo-stamp-stable").write_text(f"{now - 100}\n")
    stamp.serve(when=now - 200)
    assert stamp.check()[1]["result"] == "replay"
    stamp.serve(when=now - 50)
    assert stamp.check()[1]["result"] == "ok"
    assert stamp.seen()["time"] == now - 50  # rewritten in the new form


@pytest.mark.parametrize("content", ["", "garbage", "[1, 2]", '{"time": "soon"}', '{"hashed": "yes"}'])
def test_unusable_state_is_no_floor(stamp, content):
    stamp.user.parent.mkdir()
    (stamp.user.parent / "repo-stamp-stable").write_text(content)
    stamp.serve(when=1_000_000_000, db_hash=None, sig_hash=None)
    rc, verdict = stamp.check()
    # Neither a replay nor "named its database before": the old unhashed stamp passes (as stale)
    assert (rc, verdict["result"]) == (2, "stale")


def test_switching_channel_is_no_replay(stamp):
    now = int(time.time())
    stamp.use_server(BETA)
    stamp.serve(channel="beta", when=now - 60)
    assert stamp.check()[0] == 0
    # plinius-pkg channel-stable: stable was last stamped at its release, days before beta
    stamp.use_server(STABLE)
    stamp.serve(channel="stable", when=now - 5 * 86400)
    assert stamp.check()[0] == 0
    # Back on beta: an older beta stamp than the one seen there is a replay
    stamp.use_server(BETA)
    stamp.serve(channel="beta", when=now - 3600)
    assert stamp.check()[1]["result"] == "replay"
    assert stamp.seen("beta")["time"] == now - 60 and stamp.seen("stable")["time"] == now - 5 * 86400


# --- Age ------------------------------------------------------------------------------------------

def test_stamp_older_than_30_days_is_stale_but_genuine(stamp):
    stamp.clock.now = 2_000_000_000
    stamp.serve(when=2_000_000_000 - 31 * 86400 - 10)
    rc, verdict = stamp.check()
    assert (rc, verdict["result"], verdict["days"]) == (2, "stale", 31)
    assert verdict["message"] == "the stamp is 31 days old"
    # Still the newest genuine one: remembered
    assert stamp.seen()["time"] == 2_000_000_000 - 31 * 86400 - 10
    stamp.serve(when=2_000_000_000 - 30 * 86400)
    assert stamp.check()[1]["result"] == "ok"


def test_stamp_from_the_future_is_zero_days_old(stamp):
    stamp.serve(when=int(time.time()) + 86400 * 3)
    assert stamp.check()[1]["days"] == 0


# --- Signature ------------------------------------------------------------------------------------

@pytest.mark.parametrize("change", ["tampered", "attacker", "garbage", "truncated", "swapped", "empty"])
def test_unsigned_or_badly_signed_stamp(stamp, change):
    when = int(time.time()) - 60
    data = stamp.serve(when=when)
    files = stamp.state["files"]
    if change == "tampered":
        files["plinius.stamp"] = data.replace(f"time={when}".encode(), f"time={when + 10 ** 6}".encode())
    elif change == "attacker":
        stamp.serve(when=when, by=stamp.attacker_fpr)
    elif change == "garbage":
        files["plinius.stamp.sig"] = b"\x89not a signature"
    elif change == "truncated":
        files["plinius.stamp.sig"] = files["plinius.stamp.sig"][:20]
    elif change == "swapped":
        files["plinius.stamp"], files["plinius.stamp.sig"] = files["plinius.stamp.sig"], files["plinius.stamp"]
    else:
        files["plinius.stamp.sig"] = b""
    rc, verdict = stamp.check()
    assert (rc, verdict["result"]) == (1, "unsigned")
    assert verdict["message"] == "the stamp is not signed by a trusted Plinius key"
    assert not stamp.user.parent.exists()


def test_attacker_key_in_the_keyring_is_still_not_trusted(stamp):
    (stamp.keyrings / "plinius.gpg").write_bytes(stamp.export(stamp.trusted_fpr, stamp.attacker_fpr))
    stamp.serve(by=stamp.attacker_fpr)
    assert stamp.check()[1]["result"] == "unsigned"
    stamp.serve()
    assert stamp.check()[1]["result"] == "ok"


def test_revoked_key_no_longer_counts(stamp):
    stamp.serve()
    (stamp.keyrings / "plinius-revoked").write_text(f"# leaked\n{stamp.trusted_fpr.lower()}\n")
    assert stamp.check()[1]["result"] == "unsigned"
    # A new trusted key next to the revoked one does (key rotation)
    (stamp.keyrings / "plinius.gpg").write_bytes(stamp.export(stamp.trusted_fpr, stamp.attacker_fpr))
    (stamp.keyrings / "plinius-trusted").write_text(f"{stamp.trusted_fpr}:4:\n{stamp.attacker_fpr}:4:\n")
    stamp.serve(by=stamp.attacker_fpr)
    assert stamp.check()[1]["result"] == "ok"


def test_binary_and_armored_keyrings(stamp):
    stamp.serve()
    (stamp.keyrings / "plinius.gpg").write_bytes(stamp.export(stamp.trusted_fpr, armor=False))
    assert stamp.check()[1]["result"] == "ok"
    # Armor headers and leading blank lines are skipped
    armored = stamp.export(stamp.trusted_fpr).decode()
    head, rest = armored.split("\n", 1)
    (stamp.keyrings / "plinius.gpg").write_text(f"\n\n{head}\nComment: plinius\nVersion: x\n\n{rest.lstrip()}")
    assert stamp.check()[1]["result"] == "ok"
    # Without headers the blank line may be missing too
    (stamp.keyrings / "plinius.gpg").write_text(f"{head}\n{rest.lstrip()}")
    assert stamp.check()[1]["result"] == "ok"


@pytest.mark.parametrize("keyring", [
    b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n@@@\n-----END PGP PUBLIC KEY BLOCK-----\n",  # not base64
    b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nAAAA\n",                                     # no end
    b"-----BEGIN something else\n",
    b"-----BEGIN PGP \xff\n",                                                              # not ASCII
])
def test_broken_armored_keyring_is_no_trust(stamp, keyring):
    stamp.serve()
    (stamp.keyrings / "plinius.gpg").write_bytes(keyring)
    assert stamp.check()[1]["result"] == "unsigned"


def test_signature_needs_trusted_list_and_keyring(stamp, mod):
    data = stamp.serve()
    sig = stamp.state["files"]["plinius.stamp.sig"]
    assert mod.signed(data, sig)
    os.remove(stamp.keyrings / "plinius-trusted")
    assert not mod.signed(data, sig)
    (stamp.keyrings / "plinius-trusted").write_text(f"# repo key\n\n{stamp.trusted_fpr.lower()}:4:\n")
    assert mod.signed(data, sig)
    os.remove(stamp.keyrings / "plinius.gpg")
    assert not mod.signed(data, sig)


def test_signature_with_only_the_attacker_trusted(stamp, mod):
    data = stamp.serve()
    (stamp.keyrings / "plinius-trusted").write_text(f"{stamp.attacker_fpr}:4:\n")
    assert not mod.signed(data, stamp.state["files"]["plinius.stamp.sig"])


@pytest.mark.parametrize("error", [FileNotFoundError("gpgv"), subprocess.TimeoutExpired("gpgv", 30)])
def test_signature_without_a_working_gpgv(stamp, mod, monkeypatch, error):
    data = stamp.serve()

    def run(cmd, **kwargs):
        raise error
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert not mod.signed(data, stamp.state["files"]["plinius.stamp.sig"])


def test_gpgv_success_without_a_valid_signature_line(stamp, mod, monkeypatch):
    # Only VALIDSIG of a trusted key counts, not the exit code alone
    data = stamp.serve()
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, f"[GNUPG:] NEWSIG\n[GNUPG:] GOODSIG {stamp.trusted_fpr[-16:]} repo\n[GNUPG:] VALIDSIG\n", ""))
    assert not mod.signed(data, b"sig")


@pytest.fixture(scope="module")
def subkey(tmp_path_factory):
    """A primary key that signs with a subkey: (keys, primary fingerprint, subkey fingerprint)."""
    keys = TestKeys(tmp_path_factory.mktemp("gpg-subkey") / "home")
    try:
        primary = keys.generate("subkeyed")
        keys.gpg("--quick-add-key", primary, "ed25519", "sign", "never")
        out = keys.gpg("--with-colons", "--list-keys", primary).decode()
        fprs = [line.split(":")[9] for line in out.splitlines() if line.startswith("fpr:")]
        yield keys, primary, fprs[-1]
    finally:
        keys.stop()


def test_signature_by_a_subkey_of_the_trusted_primary_key(stamp, mod, subkey):
    keys, primary, sub = subkey
    assert sub != primary
    data = b"channel=stable\ntime=1\n"
    sig = keys.sign(sub + "!", data)
    (stamp.keyrings / "plinius.gpg").write_bytes(keys.export(primary))
    (stamp.keyrings / "plinius-trusted").write_text(f"{primary}:4:\n")
    assert mod.signed(data, sig)


# --- The database the stamp names -----------------------------------------------------------------

def test_database_other_than_the_stamped_one_is_a_mismatch(stamp):
    stamp.serve()
    stamp.db.write_bytes(b"an older database, correctly signed")
    rc, verdict = stamp.check()
    assert (rc, verdict["result"]) == (3, "mismatch")
    assert verdict["message"] == "the database does not match the stamp"
    assert not stamp.user.parent.exists()  # nothing remembered from a stamp that did not match


def test_signature_other_than_the_stamped_one_is_a_mismatch(stamp):
    stamp.serve()
    (stamp.db.parent / "plinius.db.sig").write_bytes(b"another signature")
    assert stamp.check()[:1] == (3,)


def test_signature_hash_only_compared_when_the_signature_is_there(stamp):
    # SigLevel DatabaseOptional: pacman may have fetched no plinius.db.sig
    stamp.serve()
    os.remove(stamp.db.parent / "plinius.db.sig")
    assert stamp.check()[1]["result"] == "ok"
    # A stamp without sig_sha256 checks the database alone
    (stamp.db.parent / "plinius.db.sig").write_bytes(b"whatever")
    stamp.serve(sig_hash=None)
    assert stamp.check()[1]["result"] == "ok"


@pytest.mark.parametrize("missing", ["db", "directory"])
def test_database_that_cannot_be_read_is_unreadable_not_a_mismatch(stamp, missing):
    stamp.serve()
    os.remove(stamp.db)
    if missing == "directory":
        stamp.db.mkdir()
    rc, verdict = stamp.check()
    # A local problem says nothing about the server: no retry (exit 3) and no "mismatch"
    assert (rc, verdict["result"], verdict["db"]) == (1, "unreadable", str(stamp.db))
    error = "No such file or directory" if missing == "db" else "Is a directory"
    assert verdict["error"] == error
    assert verdict["message"] == f"the database {stamp.db} could not be read: {error}"


@pytest.mark.parametrize("text", [
    "channel=stable\n",                                         # no time
    "channel=stable\ntime=soon\n",
    "channel=stable\ntime=-5\n",
    "channel=stable\ntime=1234567890123\n",                     # more than 12 digits
    "channel=stable\ntime=1\ndb_sha256=abc\n",
    "channel=stable\ntime=1\ndb_sha256=" + "A" * 64 + "\n",     # upper case is not the format
    "channel=stable\ntime=1\ndb_sha256=" + "a" * 64 + "\nsig_sha256=xyz\n",
])
def test_malformed_stamp(stamp, text):
    stamp.serve(text=text)
    rc, verdict = stamp.check()
    assert (rc, verdict["result"], verdict["message"]) == (1, "malformed", "the stamp is malformed")


# --- Stamps without the database hash (before HASH_REQUIRED_FROM) ---------------------------------

def test_unhashed_stamp_accepted_until_a_hashed_one_was_seen(stamp):
    now = int(time.time())
    stamp.serve(when=now - 300, db_hash=None, sig_hash=None)
    assert stamp.check()[1]["result"] == "ok"
    assert stamp.seen()["hashed"] is False
    stamp.serve(when=now - 200)
    assert stamp.check()[1]["result"] == "ok"
    assert stamp.seen()["hashed"] is True
    # From now on a stamp without the database hash is a downgrade
    stamp.serve(when=now - 100, db_hash=None, sig_hash=None)
    rc, verdict = stamp.check()
    assert (rc, verdict["result"]) == (1, "unhashed")
    assert verdict["message"] == "the stamp does not name its database"


def test_hashed_seen_by_root_counts_for_every_user(stamp, mod, monkeypatch):
    stamp.serve(when=int(time.time()) - 300)
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    stamp.check(user_state=False)
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    stamp.serve(when=int(time.time()) - 100, db_hash=None, sig_hash=None)
    assert stamp.check()[1]["result"] == "unhashed"


def test_from_the_deadline_on_every_stamp_names_its_database(stamp, mod, monkeypatch):
    monkeypatch.setattr(mod, "HASH_REQUIRED_FROM", int(time.time()) - 1)
    stamp.serve(db_hash=None, sig_hash=None)
    assert stamp.check()[1]["result"] == "unhashed"
    stamp.serve()
    assert stamp.check()[1]["result"] == "ok"


def test_deadline_is_the_first_of_december_2026():
    # Documented in the PKGBUILD and the script: tools/lib.sh stamps the hash since 0.12-23
    mod = load_script(SCRIPT, "repo_stamp_deadline")
    assert mod.HASH_REQUIRED_FROM == 1796083200  # 2026-12-01T00:00:00Z


# --- Answers of the server -------------------------------------------------------------------------

def test_missing_stamp_before_and_after_the_first_one(stamp):
    # Before the first stamped publication: nothing to warn about
    rc, verdict = stamp.check()
    assert (rc, verdict["result"]) == (0, "none")
    assert verdict["message"] == "the update server has no stamp yet"
    stamp.serve()
    assert stamp.check()[0] == 0
    # Once one was seen, a missing one is "no stamp could be had" (exit 4)
    stamp.state["files"].clear()
    rc, verdict = stamp.check()
    assert (rc, verdict["result"]) == (4, "missing")


def test_missing_signature_counts_as_missing_stamp(stamp):
    stamp.serve()
    stamp.check()
    del stamp.state["files"]["plinius.stamp.sig"]
    assert stamp.check()[1]["result"] == "missing"


def test_missing_stamp_after_the_deadline_is_a_warning_on_a_new_device(stamp, mod, monkeypatch):
    monkeypatch.setattr(mod, "HASH_REQUIRED_FROM", int(time.time()) - 1)
    assert stamp.check()[:1] == (4,)


def test_http_error(stamp):
    stamp.state["files"]["plinius.stamp"] = urllib.error.HTTPError("u", 500, "x", {}, None)
    rc, verdict = stamp.check()
    assert (rc, verdict["result"], verdict["status"]) == (4, "http", 500)
    assert verdict["message"] == "the update server refused its stamp (HTTP 500)"
    assert stamp.clock.slept == []  # an HTTP answer is not retried


@pytest.mark.parametrize("error", [urllib.error.URLError("offline"), TimeoutError(), ConnectionResetError(),
                                   ValueError("bad URL")])
def test_unreachable_after_one_retry(stamp, mod, error):
    stamp.state["files"]["plinius.stamp"] = error
    rc, verdict = stamp.check()
    assert (rc, verdict["result"]) == (4, "unreachable")
    assert verdict["message"] == "the update server did not hand out its stamp"
    assert stamp.clock.slept == [mod.RETRY_DELAY]
    assert stamp.state["calls"] == [STABLE + "/plinius.stamp"] * 2


def test_a_second_try_that_works_is_checked_as_usual(stamp, mod):
    data = stamp.serve()
    answers = [ConnectionResetError(), data]
    stamp.state["files"]["plinius.stamp"] = lambda: answers.pop(0)
    assert stamp.check()[1]["result"] == "ok"
    assert stamp.clock.slept == [mod.RETRY_DELAY]


def test_stamp_is_read_up_to_its_size_limit(stamp, mod):
    stamp.serve()
    stamp.state["files"]["plinius.stamp"] += b"x" * (2 * mod.MAX_SIZE)
    # Only the first MAX_SIZE bytes are read: the signature no longer matches them
    assert stamp.check()[1]["result"] == "unsigned"


# --- State files ----------------------------------------------------------------------------------

def test_state_that_cannot_be_written_is_reported_not_fatal(stamp):
    stamp.user.parent.parent.mkdir(parents=True, exist_ok=True)
    stamp.user.parent.write_text("a file where the directory should be")
    stamp.serve()
    rc, verdict = stamp.check()
    assert (rc, verdict["result"]) == (0, "ok")
    assert "plinius-repo-stamp: cannot write" in stamp.err


def test_state_is_replaced_atomically(stamp):
    stamp.serve()
    stamp.check()
    stamp.serve(when=int(time.time()))
    stamp.check()
    assert sorted(p.name for p in stamp.user.parent.iterdir()) == ["repo-stamp-stable"]


# --- Command line ---------------------------------------------------------------------------------

@pytest.mark.parametrize("argv, error", [
    ([], "usage: plinius-repo-stamp check"),
    (["verify"], "usage: plinius-repo-stamp check"),
    (["check"], "--db is missing"),
    (["check", "--db"], "--db is missing"),
    (["check", "--db", "x", "--force"], "unknown option --force"),
])
def test_command_line_errors(mod, capsys, monkeypatch, argv, error):
    monkeypatch.setattr(mod, "check", lambda *a: pytest.fail("no check"))
    assert mod.main(["plinius-repo-stamp", *argv]) == 1
    out = capsys.readouterr()
    assert error in out.err and out.out == ""


def test_user_state_without_a_value_is_none(mod, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(mod, "check", lambda db, user_state: calls.append((db, user_state)) or {"result": "skip"})
    assert mod.main(["plinius-repo-stamp", "check", "--db", "/x/plinius.db", "--user-state"]) == 0
    assert calls == [("/x/plinius.db", None)]


@pytest.mark.parametrize("verdict, code, message", [
    ({"result": "ok", "channel": "beta"}, 0, "the stamp of beta matches the database"),
    ({"result": "skip"}, 0, "no https server for [plinius], nothing to check"),
    ({"result": "none"}, 0, "the update server has no stamp yet"),
    ({"result": "stale", "days": 40}, 2, "the stamp is 40 days old"),
    ({"result": "mismatch"}, 3, "the database does not match the stamp"),
    ({"result": "unreachable"}, 4, "the update server did not hand out its stamp"),
    ({"result": "http", "status": 503}, 4, "the update server refused its stamp (HTTP 503)"),
    ({"result": "missing"}, 4, "the update server has no stamp"),
    ({"result": "unsigned"}, 1, "the stamp is not signed by a trusted Plinius key"),
    ({"result": "malformed"}, 1, "the stamp is malformed"),
    ({"result": "channel", "got": None, "want": "stable"}, 1, "the stamp is of channel None, not stable"),
    ({"result": "replay"}, 1, "the stamp is older than one seen before"),
    ({"result": "unhashed"}, 1, "the stamp does not name its database"),
    ({"result": "unreadable", "db": "/d", "error": "Permission denied"}, 1,
     "the database /d could not be read: Permission denied"),
])
def test_exit_code_and_message_of_every_verdict(mod, monkeypatch, capsys, verdict, code, message):
    # plinius-pkg acts on the exit code (0/2 install, 3 fetch again, 4 warn and install, else stop),
    # plinius-software on "result"
    monkeypatch.setattr(mod, "check", lambda db, user_state: dict(verdict))
    assert mod.main(["plinius-repo-stamp", "check", "--db", "/d"]) == code
    assert json.loads(capsys.readouterr().out) == dict(verdict, message=message)
    assert mod.main(["plinius-repo-stamp", "check", "--db", "/d", "--text"]) == code
    assert capsys.readouterr().out == f"plinius-repo-stamp: {message}\n"


def test_every_result_has_a_message(mod):
    results = {"ok", "skip", "none", "unreachable", "missing", "http", "unsigned", "malformed",
               "channel", "replay", "unhashed", "mismatch", "unreadable", "stale"}
    assert set(mod.MESSAGES) == results and set(mod.EXIT) <= results


def test_run_as_a_program(monkeypatch, capsys):
    # The usage error needs no path of the system
    monkeypatch.setattr(sys, "argv", ["plinius-repo-stamp"])
    with pytest.raises(SystemExit) as e:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert e.value.code == 1
    assert "usage:" in capsys.readouterr().err
