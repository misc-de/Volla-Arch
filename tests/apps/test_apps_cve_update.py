# plinius-cve-update (pkgbuilds/plinius-security): signature and age checks of the central
# assessments, the kernel list, matching the tracker list against the pacman database.
#
# Nothing reaches the network (urllib.request.urlopen is replaced), every path the program
# reads or writes points into tmp_path, the signing keys are throw-away keys (apps_gpgkeys).

import io
import json
import shutil
import sys
import urllib.error

import pytest

from apps_gpgkeys import gpg_keys  # noqa: F401  (fixture)
from conftest import PKG, load_script

SCRIPT = PKG / "plinius-security" / "plinius-cve-update"
needs_vercmp = pytest.mark.skipif(shutil.which("vercmp") is None, reason="no vercmp (pacman)")


@pytest.fixture(scope="module")
def mod():
    return load_script(SCRIPT)


class Response(io.BytesIO):
    """What urlopen returns: a readable body that is also a context manager."""


class FakeNet:
    """urlopen stand-in: answers by URL from a dict (bytes, or an exception to raise)."""

    def __init__(self):
        self.answers = {}
        self.calls = []

    def __call__(self, req, timeout=None):
        assert req.get_header("User-agent") == "Plinius Arch Linux"
        url = req.full_url
        self.calls.append(url)
        answer = self.answers.get(url, urllib.error.URLError("no route to host"))
        if callable(answer):
            answer = answer()
        if isinstance(answer, BaseException):
            raise answer
        return Response(answer)


def http_error(url, code):
    return urllib.error.HTTPError(url, code, "error", {}, None)


@pytest.fixture
def env(mod, tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    local = tmp_path / "local"
    local.mkdir()
    for name, value in (("STATE_DIR", state), ("KEYRING", tmp_path / "plinius.gpg"),
                        ("TRUSTED", tmp_path / "trusted"), ("REVOKED", tmp_path / "revoked"),
                        ("ASSESSMENTS", tmp_path / "packaged.json"), ("PACMAN_LOCAL", local)):
        monkeypatch.setattr(mod, name, str(value))
    net = FakeNet()
    monkeypatch.setattr(mod.urllib.request, "urlopen", net)
    monkeypatch.setattr(mod, "NETWORK_RETRY_DELAY", 0)
    return tmp_path, net


@pytest.fixture
def signed(mod, env, gpg_keys):
    """The trusted test key installed as the repository key; returns a signing helper."""
    tmp, _net = env
    keys, trusted, attacker = gpg_keys
    (tmp / "plinius.gpg").write_bytes(keys.export(trusted))
    (tmp / "trusted").write_text(f"# repo keys\n{trusted.lower()}:4:\n")
    (tmp / "revoked").write_text("")

    class Signer:
        fpr, other = trusted, attacker

        @staticmethod
        def sign(data, by=trusted):
            return keys.sign(by, data)
    return Signer


def central(updated, **assessments):
    return json.dumps({"updated": updated, "assessments": assessments}).encode()


# --- Signature check (verify) ----------------------------------------------------------------

def test_valid_signature_is_accepted(mod, signed):
    data = central(100)
    assert mod.verify(data, signed.sign(data)) is True


def test_tampered_data_is_rejected(mod, signed):
    data = central(100, **{"CVE-1": {"status": "affected"}})
    sig = signed.sign(data)
    assert mod.verify(data.replace(b"affected", b"fixed\x00\x00\x00"), sig) is False
    assert mod.verify(data + b" ", sig) is False


def test_truncated_or_garbage_signature_is_rejected(mod, signed):
    data = central(100)
    sig = signed.sign(data)
    assert mod.verify(data, sig[: len(sig) // 2]) is False
    assert mod.verify(data, b"") is False
    assert mod.verify(data, b"\x00" * len(sig)) is False


def test_signature_by_an_untrusted_key_is_rejected_even_if_in_the_keyring(mod, signed, env, gpg_keys):
    # gpgv itself would accept any key of the keyring: the fingerprint check must refuse it
    tmp, _net = env
    keys, trusted, attacker = gpg_keys
    (tmp / "plinius.gpg").write_bytes(keys.export(trusted, attacker))
    data = central(100)
    assert mod.verify(data, signed.sign(data, by=attacker)) is False
    assert mod.verify(data, signed.sign(data)) is True


def test_revoked_key_is_rejected(mod, signed, env):
    tmp, _net = env
    (tmp / "revoked").write_text(signed.fpr + "\n")
    data = central(100)
    assert mod.verify(data, signed.sign(data)) is False


def test_no_trusted_key_or_no_keyring_rejects(mod, signed, env):
    tmp, _net = env
    data = central(100)
    sig = signed.sign(data)
    (tmp / "trusted").write_text("# empty\n")
    assert mod.verify(data, sig) is False
    (tmp / "trusted").write_text(signed.fpr + ":4:\n")
    (tmp / "plinius.gpg").unlink()
    assert mod.verify(data, sig) is False
    (tmp / "plinius.gpg").write_text("not a key\n")
    assert mod.verify(data, sig) is False


def test_fingerprints_formats(mod, tmp_path):
    f = tmp_path / "list"
    f.write_text("# comment\nabc:4:\n\nDEF\n  ghi:3:  \n")
    assert mod.fingerprints(str(f)) == {"ABC", "DEF", "GHI"}
    assert mod.fingerprints(str(tmp_path / "missing")) == set()


def test_dearmor_strips_headers_and_checksum(mod):
    armored = ("-----BEGIN PGP PUBLIC KEY BLOCK-----\nComment: test\n\n"
               "aGVsbG8g\nd29ybGQ=\n=abcd\n-----END PGP PUBLIC KEY BLOCK-----\n")
    assert mod.dearmor(armored) == b"hello world"
    with pytest.raises(StopIteration):
        mod.dearmor("no armor here")
    with pytest.raises(ValueError):
        mod.dearmor("-----BEGIN PGP X-----\n\n@@@@\n-----END PGP X-----\n")


# --- Central assessments: download, replay protection, kept copy -----------------------------

def serve_central(net, signer, data, sig=None):
    net.answers[MOD_URL] = data
    net.answers[MOD_URL + ".sig"] = signer.sign(data) if sig is None else sig


MOD_URL = "https://plinius.cais.de/security/assessments.json"


def test_download_central_keeps_a_signed_list(mod, env, signed):
    tmp, net = env
    data = central(200, **{"CVE-1": {"status": "fixed", "date": "2026-11-01"}})
    serve_central(net, signed, data)
    mod.download_central()
    assert (tmp / "state" / "assessments.json").read_bytes() == data
    assert mod.load_central()["updated"] == 200


def test_download_central_refuses_a_bad_signature_and_keeps_nothing(mod, env, signed):
    tmp, net = env
    data = central(200)
    serve_central(net, signed, data, sig=signed.sign(central(201)))
    with pytest.raises(ValueError, match="signature"):
        mod.download_central()
    assert not (tmp / "state" / "assessments.json").exists()


def test_download_central_refuses_an_older_signed_list(mod, env, signed):
    # A correctly signed but old copy must not bring back outdated assessments (replay)
    tmp, net = env
    new = central(300, **{"CVE-1": {"status": "affected"}})
    serve_central(net, signed, new)
    mod.download_central()
    old = central(200, **{"CVE-1": {"status": "not_affected"}})
    serve_central(net, signed, old)
    with pytest.raises(ValueError, match="older"):
        mod.download_central()
    assert (tmp / "state" / "assessments.json").read_bytes() == new


def test_download_central_same_time_is_not_rewritten(mod, env, signed):
    tmp, net = env
    first = central(300)
    serve_central(net, signed, first)
    mod.download_central()
    serve_central(net, signed, central(300, **{"CVE-9": {"status": "fixed"}}))
    mod.download_central()
    assert (tmp / "state" / "assessments.json").read_bytes() == first


@pytest.mark.parametrize("payload", [b'{"assessments": {}}', b'{"updated": "300"}', b"[1, 2]",
                                     b'{"updated": 3.5}'])
def test_download_central_needs_an_integer_publication_time(mod, env, signed, payload):
    tmp, net = env
    serve_central(net, signed, payload)
    with pytest.raises(ValueError):
        mod.download_central()
    assert not (tmp / "state" / "assessments.json").exists()


def test_download_central_refuses_signed_non_json(mod, env, signed):
    tmp, net = env
    serve_central(net, signed, b"\xff not json")
    with pytest.raises(ValueError):
        mod.download_central()
    assert not (tmp / "state" / "assessments.json").exists()


def test_fetch_cuts_off_oversized_answers(mod, env, monkeypatch):
    _tmp, net = env
    monkeypatch.setattr(mod, "CENTRAL_LIMIT", 10)
    net.answers["https://x/a"] = b"x" * 11
    with pytest.raises(ValueError, match="larger"):
        mod.fetch("https://x/a", 10)
    net.answers["https://x/b"] = b"x" * 10
    assert mod.fetch("https://x/b", 10) == b"x" * 10


def test_kept_central_copy_is_checked_again_when_read(mod, env, signed):
    tmp, net = env
    data = central(300, **{"CVE-1": {"status": "not_affected"}})
    serve_central(net, signed, data)
    mod.download_central()
    assert mod.load_central() is not None
    # Someone with write access to the state directory changes the kept copy
    (tmp / "state" / "assessments.json").write_bytes(data.replace(b"300", b"999"))
    assert mod.load_central() is None
    (tmp / "state" / "assessments.json.sig").unlink()
    assert mod.load_central() is None


def test_a_tampered_kept_copy_does_not_block_a_valid_download(mod, env, signed):
    tmp, net = env
    (tmp / "state" / "assessments.json").write_bytes(central(10 ** 12))
    (tmp / "state" / "assessments.json.sig").write_bytes(b"junk")
    data = central(300)
    serve_central(net, signed, data)
    mod.download_central()
    assert (tmp / "state" / "assessments.json").read_bytes() == data


# --- Assessments: parsing and merging --------------------------------------------------------

def test_parse_assessments_keeps_only_valid_entries(mod):
    data = {"assessments": {"CVE-1": {"status": "fixed"}, "CVE-2": {"status": "bogus"},
                            "CVE-3": "fixed", "AVG-4": {"status": "mitigated"}},
            "extra": [{"cve": "CVE-5", "status": "affected"}, {"status": "affected"},
                      {"cve": "CVE-6", "status": "nope"}, "junk"]}
    assessments, extra = mod.parse_assessments(data)
    assert set(assessments) == {"CVE-1", "AVG-4"}
    assert set(extra) == {"CVE-5"}
    assert mod.parse_assessments([1]) == ({}, {})
    assert mod.parse_assessments({"assessments": [], "extra": {}}) == ({}, {})


def test_merge_later_date_wins_central_on_tie(mod):
    packaged = {"assessments": {
        "CVE-1": {"status": "affected", "date": "2026-11-05"},
        "CVE-2": {"status": "affected", "date": "2026-11-01"},
        "CVE-3": {"status": "affected", "date": "2026-11-01"},
        "CVE-4": {"status": "affected", "date": "2026-11-01"}}}
    central_ = {"assessments": {
        "CVE-1": {"status": "fixed", "date": "2026-11-01"},
        "CVE-2": {"status": "fixed", "date": "2026-11-02"},
        "CVE-3": {"status": "fixed", "date": "2026-11-01"},
        "CVE-5": {"status": "mitigated"}},
        "extra": [{"cve": "CVE-9", "status": "affected"}]}
    assessments, extra = mod.merge_assessments(packaged, central_)
    assert {k: v["status"] for k, v in assessments.items()} == {
        "CVE-1": "affected", "CVE-2": "fixed", "CVE-3": "fixed", "CVE-4": "affected",
        "CVE-5": "mitigated"}
    assert [e["cve"] for e in extra] == ["CVE-9"]
    assert mod.merge_assessments(None, None) == ({}, [])


# --- Matching the tracker against installed packages -----------------------------------------

def write_desc(local, name, version, provides=(), replaces=()):
    d = local / f"{name}-{version}"
    d.mkdir()
    text = f"%NAME%\n{name}\n\n%VERSION%\n{version}\n\n"
    if provides:
        text += "%PROVIDES%\n" + "\n".join(provides) + "\n\n"
    if replaces:
        text += "%REPLACES%\n" + "\n".join(replaces) + "\n\n"
    (d / "desc").write_text(text)


def test_installed_packages_reads_versions_and_provides(mod, env):
    tmp, _net = env
    local = tmp / "local"
    write_desc(local, "openssl", "3.5.0-1")
    write_desc(local, "phosh-plinius", "0.57.0-98", provides=["phosh=0.57.0", "libphosh.so=0-64"])
    write_desc(local, "foo-git", "1.0-1", provides=["foo"], replaces=["oldfoo"])
    (local / "ALPM_DB_VERSION").write_text("9\n")  # a file, not a package directory
    (local / "broken").mkdir()                       # no desc
    versions, provides = mod.installed_packages()
    assert versions == {"openssl": "3.5.0-1", "phosh-plinius": "0.57.0-98", "foo-git": "1.0-1"}
    assert provides["phosh"] == ("phosh-plinius", "0.57.0")
    assert provides["foo"] == ("foo-git", "1.0-1")
    assert provides["oldfoo"] == ("foo-git", "1.0-1")


TRACKER = [
    {"name": "AVG-1", "packages": ["openssl"], "status": "Vulnerable", "fixed": "3.5.1-1",
     "affected": "3.5.0-1", "issues": ["CVE-A", "CVE-B"], "severity": "High", "type": "dos"},
    {"name": "AVG-2", "packages": ["openssl"], "status": "Fixed", "fixed": "3.4.0-1",
     "issues": ["CVE-OLD"]},
    {"name": "AVG-3", "packages": ["phosh"], "status": "Vulnerable", "fixed": "0.58.0-1",
     "issues": ["CVE-P"]},
    {"name": "AVG-4", "packages": ["vim"], "status": "Vulnerable", "fixed": None,
     "issues": ["CVE-V"]},
    {"name": "AVG-5", "packages": ["openssl"], "status": "Not affected", "issues": ["CVE-N"]},
    {"name": "AVG-6", "packages": ["openssl"], "status": "Vulnerable", "fixed": None,
     "issues": ["CVE-G1", "CVE-G2"]},
]


@needs_vercmp
def test_match_lists_unfixed_issues_of_installed_and_provided_packages(mod):
    versions = {"openssl": "3.5.0-1", "phosh-plinius": "0.57.0-98"}
    provides = {"phosh": ("phosh-plinius", "0.57.0")}
    assessments = {"CVE-B": {"status": "not_affected"}, "AVG-6": {"status": "fixed"},
                   "CVE-A": {"status": "mitigated", "note": "n"}}
    issues = mod.match(TRACKER, versions, provides, assessments)
    by_cve = {i["cve"]: i for i in issues}
    # CVE-OLD fixed by the installed version, CVE-B and AVG-6 assessed harmless, vim missing,
    # AVG-5 not affected
    assert set(by_cve) == {"CVE-A", "CVE-P"}
    assert by_cve["CVE-A"]["assessment"]["status"] == "mitigated"
    assert by_cve["CVE-P"]["package"] == "phosh-plinius"
    assert by_cve["CVE-P"]["installed"] == "0.57.0"


@needs_vercmp
def test_match_uses_pacman_version_order(mod):
    group = [{"name": "AVG-1", "packages": ["p"], "status": "Vulnerable", "fixed": "1:1.0-1",
              "issues": ["CVE-E"]}]
    # An epoch beats any version without one; 1.10 is newer than 1.9
    assert [i["cve"] for i in mod.match(group, {"p": "9.9-1"}, {}, {})] == ["CVE-E"]
    assert mod.match(group, {"p": "1:1.0-1"}, {}, {}) == []
    group[0]["fixed"] = "1.10-1"
    assert len(mod.match(group, {"p": "1.9-1"}, {}, {})) == 1
    assert mod.match(group, {"p": "1.10-2"}, {}, {}) == []


# --- Kernel list ------------------------------------------------------------------------------

def test_version_numbers_and_fixed_by_version(mod):
    assert mod.version_numbers("6.1.192-plinius4") == (6, 1, 192)
    assert mod.version_numbers("6.1.rc1") == (6, 1)
    assert mod.version_numbers(None) == ()
    assert mod.fixed_by_version("6.1.192", "6.1.190") is True
    assert mod.fixed_by_version("6.1.192", "6.1.192") is True
    assert mod.fixed_by_version("6.1.192", "6.1.193") is False
    # Another branch says nothing about this one
    assert mod.fixed_by_version("6.1.192", "6.6.10") is False
    assert mod.fixed_by_version("6.1.192", "5.15.200") is False
    assert mod.fixed_by_version("6.1.192", "6.1") is False
    assert mod.fixed_by_version("6.1.192", None) is False


def kernel_list(issues, release="6.1.192-plinius4", generated=1000, **extra):
    return {"version": 1, "release": release, "generated": generated, "issues": issues, **extra}


def test_kernel_issues_scoring_and_assessments(mod):
    data = kernel_list([
        {"cve": "CVE-HIGH", "score": 7.8, "cvss": "CVSS:3.1/AV:L/AC:L", "title": "t",
         "branch_fixed": True, "fixed": "6.1.200"},
        {"cve": "CVE-CRIT", "score": 9.8, "cvss": "CVSS:3.1/AV:N/AC:L"},
        {"cve": "CVE-LOW", "score": 5.5},
        {"cve": "CVE-NOSCORE", "score": "high"},
        {"cve": "CVE-HIDDEN", "score": 9.0},
        {"cve": "CVE-SKIP", "score": 9.0},
        {"cve": "CVE-NEWER", "score": 9.0, "fixed": "6.6.5"},
        {"cve": "CVE-NEWER-ASSESSED", "score": 8.0, "fixed": "6.6.5"},
        "junk", {"score": 9.0}, {"cve": 5},
    ], counts={"affected": 900, "built": 300})
    assessments = {"CVE-HIDDEN": {"status": "not_affected"},
                   "CVE-NEWER-ASSESSED": {"status": "affected"}}
    listed, summary = mod.kernel_issues(data, "6.1.192", assessments, {"CVE-SKIP"})
    by_cve = {i["cve"]: i for i in listed}
    assert set(by_cve) == {"CVE-HIGH", "CVE-CRIT", "CVE-NEWER-ASSESSED"}
    assert by_cve["CVE-CRIT"]["severity"] == "Critical"
    assert by_cve["CVE-CRIT"]["type"] == "remote"
    assert by_cve["CVE-HIGH"]["severity"] == "High"
    assert by_cve["CVE-HIGH"]["type"] == "local"
    assert by_cve["CVE-HIGH"]["fixed"] == "6.1.200"
    assert by_cve["CVE-NEWER-ASSESSED"]["fixed"] == "6.6.5 (not in the 6.1 branch)"
    assert summary == {"generated": 1000, "affected": 900, "built": 300, "listed": 3,
                       "other": 2, "assessed_harmless": 1, "not_in_branch": 1}


def test_valid_kernel_cves(mod):
    good = kernel_list([])
    assert mod.valid_kernel_cves(good, "6.1.192-plinius4")
    assert not mod.valid_kernel_cves(good, "6.1.193-plinius4")
    assert not mod.valid_kernel_cves({**good, "version": 2}, "6.1.192-plinius4")
    assert not mod.valid_kernel_cves({**good, "generated": "1000"}, "6.1.192-plinius4")
    assert not mod.valid_kernel_cves({**good, "issues": {}}, "6.1.192-plinius4")
    assert not mod.valid_kernel_cves([good], "6.1.192-plinius4")


def kernel_url(release):
    return "https://plinius.cais.de/security/kernel/" + release + ".json"


def test_download_kernel_cves_quotes_the_release_and_refuses_older_lists(mod, env):
    tmp, net = env
    release = "6.1.192+/x"
    url = kernel_url("6.1.192%2B%2Fx")
    net.answers[url] = json.dumps(kernel_list([], release=release, generated=2000)).encode()
    mod.download_kernel_cves(release)
    assert net.calls == [url]
    assert mod.load_kernel_cves(release)["generated"] == 2000
    net.answers[url] = json.dumps(kernel_list([{"cve": "X"}], release=release,
                                              generated=1999)).encode()
    with pytest.raises(ValueError, match="older"):
        mod.download_kernel_cves(release)
    assert mod.load_kernel_cves(release)["issues"] == []
    # A list for another release is not taken
    net.answers[url] = json.dumps(kernel_list([], release="other", generated=3000)).encode()
    with pytest.raises(ValueError, match="unexpected"):
        mod.download_kernel_cves(release)


def test_network_error_classification(mod):
    assert mod.network_error(urllib.error.URLError("x"))
    assert mod.network_error(TimeoutError())
    assert mod.network_error(ConnectionResetError())
    assert not mod.network_error(http_error("u", 404))
    assert not mod.network_error(ValueError("bad json"))


# --- main: the whole run ---------------------------------------------------------------------

TRACKER_URL = "https://security.archlinux.org/issues/all.json"
RELEASE = "6.1.192-plinius4"


@pytest.fixture
def run(mod, env, monkeypatch):
    tmp, net = env
    monkeypatch.setattr(mod, "kernel_info", lambda: {"release": RELEASE, "security_patch": None,
                                                     "vendor_security_patch": None})
    monkeypatch.setattr(mod, "boot_id", lambda: "boot-1")
    write_desc(tmp / "local", "openssl", "3.5.0-1")

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["plinius-cve-update", *args])
        assert mod.main() == 0
        return json.loads((tmp / "state" / "cve.json").read_text())
    return run


@needs_vercmp
def test_main_full_run(mod, env, signed, run):
    tmp, net = env
    net.answers[TRACKER_URL] = json.dumps(TRACKER).encode()
    serve_central(net, signed, json.dumps({
        "updated": 500,
        "assessments": {"CVE-B": {"status": "not_affected", "date": "2026-11-10"}},
        "extra": [
            {"cve": "CVE-K1", "package": "kernel", "status": "affected", "fixed": "6.1.190"},
            {"cve": "CVE-K2", "package": "kernel", "status": "mitigated", "fixed": "6.1.199",
             "severity": "High"},
            {"cve": "CVE-K3", "package": "kernel", "status": "fixed"}]}).encode())
    net.answers[kernel_url(RELEASE)] = json.dumps(kernel_list([
        {"cve": "CVE-K2", "score": 9.9}, {"cve": "CVE-K4", "score": 7.0}])).encode()
    (tmp / "packaged.json").write_text(json.dumps(
        {"assessments": {"CVE-B": {"status": "affected", "date": "2026-11-01"}}}))
    result = run()
    assert result["error"] is None and result["kernel"]["cves_error"] is None
    assert result["assessments_updated"] == 500
    cves = {i["cve"] for i in result["issues"]}
    # CVE-K1 is fixed by the running version, CVE-K3 assessed fixed, CVE-B by the newer central
    assert cves == {"CVE-A", "CVE-G1", "CVE-G2", "CVE-K2"}
    k2 = next(i for i in result["issues"] if i["cve"] == "CVE-K2")
    assert k2["installed"] == "6.1.192" and k2["assessment"]["status"] == "mitigated"
    # CVE-K2 is in "issues" through its extra entry, not twice
    assert [i["cve"] for i in result["kernel"]["cves"]["issues"]] == ["CVE-K4"]
    assert not (tmp / "state" / "kernel-unlisted").exists()


@needs_vercmp
def test_main_offline_uses_kept_data_and_never_downloads(mod, env, run):
    tmp, net = env
    result = run("--offline")
    assert net.calls == []
    assert result["error"] == "no tracker data yet" and result["issues"] == []
    (tmp / "state" / "arch-issues.json").write_text(json.dumps({"downloaded": 42, "issues": TRACKER}))
    result = run("--offline")
    assert net.calls == []
    assert result["downloaded"] == 42 and result["error"] is None
    assert "CVE-A" in {i["cve"] for i in result["issues"]}


@needs_vercmp
def test_main_retries_only_network_errors(mod, env, run):
    tmp, net = env
    (tmp / "state" / "arch-issues.json").write_text(json.dumps({"downloaded": 42, "issues": TRACKER}))
    # Tracker: no network at all; central: the server answers 500 (no retry); kernel: 404
    net.answers[MOD_URL] = http_error(MOD_URL, 500)
    net.answers[kernel_url(RELEASE)] = http_error(kernel_url(RELEASE), 404)
    result = run()
    assert net.calls.count(TRACKER_URL) == mod.NETWORK_TRIES
    assert net.calls.count(MOD_URL) == 1
    assert net.calls.count(kernel_url(RELEASE)) == 1
    assert "no route to host" in result["error"]
    assert result["downloaded"] == 42  # the kept list is used
    assert "404" in result["kernel"]["cves_error"]
    # The server answered: noted for the dispatcher, with the boot it was asked in
    assert (tmp / "state" / "kernel-unlisted").read_text() == f"{RELEASE} boot-1\n"


@needs_vercmp
def test_main_kernel_list_unreachable_leaves_no_unlisted_mark(mod, env, run):
    tmp, net = env
    net.answers[TRACKER_URL] = b"[]"
    result = run()
    assert result["error"] is None
    assert net.calls.count(kernel_url(RELEASE)) == mod.NETWORK_TRIES
    assert not (tmp / "state" / "kernel-unlisted").exists()


@needs_vercmp
def test_main_unexpected_tracker_data_is_an_error_and_keeps_the_old_list(mod, env, run):
    tmp, net = env
    (tmp / "state" / "arch-issues.json").write_text(json.dumps({"downloaded": 42, "issues": TRACKER}))
    net.answers[TRACKER_URL] = b'{"not": "a list"}'
    result = run()
    assert net.calls.count(TRACKER_URL) == 1
    assert result["error"] == "unexpected tracker data"
    assert json.loads((tmp / "state" / "arch-issues.json").read_text())["downloaded"] == 42


def test_main_kernel_info_mode_writes_only_kernel_json(mod, env, monkeypatch):
    tmp, net = env
    monkeypatch.setattr(mod, "read_kernel_info", lambda: {"release": "r", "security_patch": "2026-01-05",
                                                          "vendor_security_patch": None})
    monkeypatch.setattr(sys, "argv", ["plinius-cve-update", "--kernel-info"])
    assert mod.main() == 0
    assert json.loads((tmp / "state" / "kernel.json").read_text())["security_patch"] == "2026-01-05"
    assert sorted(p.name for p in (tmp / "state").iterdir()) == ["kernel.json"]
    assert net.calls == []


def test_kernel_info_falls_back_to_the_saved_file_of_the_same_release(mod, env, monkeypatch):
    tmp, _net = env
    monkeypatch.setattr(mod, "read_kernel_info", lambda: {"release": "r1", "security_patch": None,
                                                          "vendor_security_patch": None})
    saved = {"release": "r1", "security_patch": "2026-01-05", "vendor_security_patch": "x"}
    (tmp / "state" / "kernel.json").write_text(json.dumps(saved))
    assert mod.kernel_info() == saved
    saved["release"] = "r0"  # from an older kernel: not used
    (tmp / "state" / "kernel.json").write_text(json.dumps(saved))
    assert mod.kernel_info()["security_patch"] is None


def test_read_prop(mod, tmp_path):
    f = tmp_path / "build.prop"
    f.write_text("ro.a=1\nro.build.version.security_patch=2026-10-05\nro.empty=\n")
    assert mod.read_prop(str(f), "ro.build.version.security_patch") == "2026-10-05"
    assert mod.read_prop(str(f), "ro.empty") is None
    assert mod.read_prop(str(f), "ro.missing") is None
    assert mod.read_prop(str(tmp_path / "none"), "ro.a") is None


# --- Smaller paths: odd package entries, the kernel's own data, kept copies, the program -------

def test_installed_packages_skips_entries_without_name_or_version(mod, env):
    tmp, _net = env
    local = tmp / "local"
    write_desc(local, "openssl", "3.5.0-1")
    (local / "half-1").mkdir()
    (local / "half-1" / "desc").write_text("%NAME%\nhalf\n\n%DESC%\nno version\n")
    (local / "nameless-1").mkdir()
    (local / "nameless-1" / "desc").write_text("%VERSION%\n1-1\n")
    versions, provides = mod.installed_packages()
    assert versions == {"openssl": "3.5.0-1"} and provides == {}


def test_kernel_info_read_as_root_wins(mod, env, monkeypatch):
    tmp, _net = env
    direct = {"release": "r1", "security_patch": None, "vendor_security_patch": "2026-09-05"}
    monkeypatch.setattr(mod, "read_kernel_info", lambda: dict(direct))
    (tmp / "state" / "kernel.json").write_text(json.dumps(
        {"release": "r1", "security_patch": "2020-01-01", "vendor_security_patch": "old"}))
    assert mod.kernel_info() == direct


@pytest.mark.parametrize("saved", [None, "{broken"])
def test_kernel_info_without_a_usable_saved_file(mod, env, monkeypatch, saved):
    tmp, _net = env
    direct = {"release": "r1", "security_patch": None, "vendor_security_patch": None}
    monkeypatch.setattr(mod, "read_kernel_info", lambda: dict(direct))
    if saved is not None:
        (tmp / "state" / "kernel.json").write_text(saved)
    assert mod.kernel_info() == direct


def test_read_kernel_info_falls_back_to_the_system_as_root_layout(mod, monkeypatch):
    props = {("/system/system/build.prop", "ro.build.version.security_patch"): "2026-10-05",
             ("/vendor/build.prop", "ro.vendor.build.security_patch"): "2026-08-01"}
    asked = []
    monkeypatch.setattr(mod, "read_prop", lambda path, key: asked.append(path) or
                        props.get((path, key)))
    info = mod.read_kernel_info()
    assert info == {"release": mod.platform.release(), "security_patch": "2026-10-05",
                    "vendor_security_patch": "2026-08-01"}
    assert asked == ["/system/build.prop", "/system/system/build.prop", "/vendor/build.prop"]


def test_merge_keeps_a_newer_packaged_extra_entry(mod):
    packaged = {"extra": [{"cve": "CVE-K", "status": "affected", "date": "2026-11-05"}]}
    central_ = {"extra": [{"cve": "CVE-K", "status": "fixed", "date": "2026-11-01"}]}
    _assessments, extra = mod.merge_assessments(packaged, central_)
    assert extra == [{"cve": "CVE-K", "status": "affected", "date": "2026-11-05"}]


def test_dearmor_without_armor_headers(mod):
    armored = ("-----BEGIN PGP PUBLIC KEY BLOCK-----\naGVsbG8g\nd29ybGQ=\n=abcd\n"
               "-----END PGP PUBLIC KEY BLOCK-----\n")
    assert mod.dearmor(armored) == b"hello world"


def keep_central(tmp, signed, data):
    (tmp / "state" / "assessments.json").write_bytes(data)
    (tmp / "state" / "assessments.json.sig").write_bytes(signed.sign(data))


def test_kept_central_copy_signed_but_not_json_is_ignored(mod, env, signed):
    tmp, _net = env
    keep_central(tmp, signed, b"\xff signed, but no JSON")
    assert mod.load_central() is None
    keep_central(tmp, signed, b"[1, 2]")
    assert mod.load_central() is None


def test_kept_central_copy_without_publication_time_is_replaced(mod, env, signed):
    tmp, net = env
    keep_central(tmp, signed, b'{"assessments": {}}')
    data = central(5)
    serve_central(net, signed, data)
    mod.download_central()
    assert (tmp / "state" / "assessments.json").read_bytes() == data


def test_boot_id(mod, monkeypatch):
    real = mod.boot_id()
    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
        assert real == f.read().strip() and len(real) == 36

    def no_proc(path, *args, **kwargs):
        raise FileNotFoundError(path)
    monkeypatch.setattr(mod, "open", no_proc, raising=False)
    assert mod.boot_id() == ""


def test_match_lists_an_issue_whose_versions_cannot_be_compared(mod, monkeypatch):
    def vercmp(a, b):
        raise ValueError("odd version")
    monkeypatch.setattr(mod, "vercmp", vercmp)
    tracker = [{"name": "AVG-1", "packages": ["openssl"], "status": "Vulnerable",
                "fixed": "3.5.1-1", "issues": ["CVE-A"]}]
    issues = mod.match(tracker, {"openssl": "weird"}, {}, {})
    # Not knowing whether the fix is installed: listed, not hidden
    assert [(i["cve"], i["installed"], i["fixed"]) for i in issues] == [
        ("CVE-A", "weird", "3.5.1-1")]


def test_as_a_program_kernel_info(tmp_path, monkeypatch):
    import runpy
    state = tmp_path / "state"
    monkeypatch.setenv("STATE_DIRECTORY", str(state))
    monkeypatch.setattr(sys, "argv", ["plinius-cve-update", "--kernel-info"])
    with pytest.raises(SystemExit) as exit_:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exit_.value.code == 0
    import platform
    saved = json.loads((state / "kernel.json").read_text())
    assert saved["release"] == platform.release()
    assert sorted(saved) == ["release", "security_patch", "vendor_security_patch"]


def test_newer_central_list_replaces_the_kept_one(mod, env, signed):
    tmp, net = env
    keep_central(tmp, signed, central(100, **{"CVE-1": {"status": "affected"}}))
    newer = central(200, **{"CVE-1": {"status": "fixed"}})
    serve_central(net, signed, newer)
    mod.download_central()
    assert (tmp / "state" / "assessments.json").read_bytes() == newer
    assert mod.load_central()["assessments"]["CVE-1"]["status"] == "fixed"
