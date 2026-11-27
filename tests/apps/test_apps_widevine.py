# plinius-widevine (pkgbuilds/plinius-widevine): the CDM download and its checks in
# plinius-widevine-update (update server answer, SHA-256, CRX3 signature, ZIP contents, ELF
# header, the ChromeOS image), the installed layout, and the shell parts (plinius-widevine-set,
# profile.d snippet).
#
# No network: urllib.request.urlopen is replaced. The CRX3 files are built here and signed
# with a throw-away RSA key; APPID is pointed at that key's ID.

import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import urllib.error
import zipfile

import pytest

from conftest import PKG, load_script

DIR = PKG / "plinius-widevine"
SCRIPT = DIR / "plinius-widevine-update"
needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="no openssl")


@pytest.fixture(scope="module")
def mod():
    return load_script(SCRIPT)


@pytest.fixture
def state(mod, tmp_path, monkeypatch):
    s = tmp_path / "state"
    s.mkdir()
    monkeypatch.setattr(mod, "STATE", s)
    return s


# --- CRX3 construction -----------------------------------------------------------------------

def varint(n):
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    return varint(number << 3 | 2) + varint(len(value)) + value


class RsaKey:
    def __init__(self, folder):
        self.pem = folder / "key.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                        "rsa_keygen_bits:2048", "-out", str(self.pem)], check=True,
                       capture_output=True)
        self.der = subprocess.run(["openssl", "pkey", "-in", str(self.pem), "-pubout",
                                   "-outform", "DER"], check=True, capture_output=True).stdout

    def sign(self, message):
        return subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(self.pem)],
                              input=message, check=True, capture_output=True).stdout


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("no openssl")
    base = tmp_path_factory.mktemp("rsa")
    (base / "a").mkdir()
    (base / "b").mkdir()
    return RsaKey(base / "a"), RsaKey(base / "b")


def elf(machine=62, size=64):
    head = b"\x7fELF\x02\x01" + b"\x00" * 10 + struct.pack("<HH", 3, machine)
    return head + b"\x00" * (size - len(head))


def make_zip(version="4.10.1", arch="x64", machine=62, manifest=None, skip=(), license_size=10):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        files = {
            "manifest.json": manifest if manifest is not None else
            json.dumps({"name": "WidevineCdm", "version": version}),
            "LICENSE": "L" * license_size,
            f"_platform_specific/linux_{arch}/libwidevinecdm.so": elf(machine),
        }
        for name, content in files.items():
            if name not in skip:
                z.writestr(name, content)
    return buf.getvalue()


def crx3(archive, signer, *, signed_id_key=None, extra_proofs=(), tamper=False, no_signed=False,
         sign_with=None):
    """A CRX3 file: magic, version, header length, header (protobuf), archive."""
    id_key = signed_id_key or signer
    signed_header = field(1, hashlib.sha256(id_key.der).digest()[:16])
    message = (b"CRX3 SignedData\x00" + struct.pack("<I", len(signed_header)) + signed_header
               + archive)
    signature = (sign_with or signer).sign(message)
    header = b"".join(field(2, field(1, k.der) + field(2, k.sign(message))) for k in extra_proofs)
    header += field(2, field(1, signer.der) + field(2, signature))
    if not no_signed:
        header += field(10000, signed_header)
    if tamper:
        archive = archive[:-1] + bytes([archive[-1] ^ 1])
    return b"Cr24" + struct.pack("<II", 3, len(header)) + header + archive


@pytest.fixture
def appid(mod, keys, monkeypatch):
    monkeypatch.setattr(mod, "APPID", mod.crx_id(keys[0].der))
    return mod.APPID


# --- protobuf parsing ------------------------------------------------------------------------

def test_protobuf_fields(mod):
    data = field(1, 150) + field(2, b"abc") + field(10000, b"")
    assert list(mod.protobuf_fields(data)) == [(1, 150), (2, b"abc"), (10000, b"")]


@pytest.mark.parametrize("data, message", [
    (b"\x80", "broken varint"),                    # varint without its end
    (b"\x80" * 11, "broken varint"),               # longer than 64 bits
    (field(2, b"abcdef")[:-2], "beyond its end"),  # truncated length-delimited field
    (b"\x0d\x00\x00\x00\x00", "wire type 5"),      # fixed32, never in a CRX3 header
    (b"\x09" + b"\x00" * 8, "wire type 1"),
])
def test_protobuf_fields_rejects_broken_data(mod, data, message):
    with pytest.raises(RuntimeError, match=message):
        list(mod.protobuf_fields(data))


def test_crx_id_is_chrome_style(mod):
    key = b"some key"
    digest = hashlib.sha256(key).hexdigest()[:32]
    assert mod.crx_id(key) == digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
    assert len(mod.crx_id(key)) == 32
    # Google's real component ID has the same shape
    assert set(mod.APPID) <= set("abcdefghijklmnop") and len(mod.APPID) == 32


# --- CRX3 verification and unpacking ---------------------------------------------------------

@needs_openssl
def test_valid_crx3_unpacks(mod, keys, appid):
    files = mod.unpack(crx3(make_zip(), keys[0]), "4.10.1", "x64", 62)
    assert set(files) == {"manifest.json", "LICENSE", "libwidevinecdm.so"}
    assert files["libwidevinecdm.so"] == elf(62)


@needs_openssl
def test_extra_proofs_by_other_keys_do_not_matter(mod, keys, appid):
    data = crx3(make_zip(), keys[0], extra_proofs=[keys[1]])
    assert mod.unpack(data, "4.10.1", "x64", 62)


@needs_openssl
def test_tampered_archive_is_rejected(mod, keys, appid):
    with pytest.raises(RuntimeError, match="signature .* is wrong"):
        mod.unpack(crx3(make_zip(), keys[0], tamper=True), "4.10.1", "x64", 62)


@needs_openssl
def test_signature_by_another_key_for_the_right_key_is_rejected(mod, keys, appid):
    # The proof names the CDM's public key, but the signature is someone else's
    with pytest.raises(RuntimeError, match="signature .* is wrong"):
        mod.unpack(crx3(make_zip(), keys[0], sign_with=keys[1]), "4.10.1", "x64", 62)


@needs_openssl
def test_crx3_signed_only_by_another_key_is_rejected(mod, keys, appid):
    # A complete, valid CRX3 of another publisher that claims the CDM's ID
    data = crx3(make_zip(), keys[1], signed_id_key=keys[0])
    with pytest.raises(RuntimeError, match="not signed by the Widevine CDM's key"):
        mod.unpack(data, "4.10.1", "x64", 62)


@needs_openssl
def test_crx3_of_another_component_is_rejected(mod, keys, appid):
    with pytest.raises(RuntimeError, match="another component"):
        mod.unpack(crx3(make_zip(), keys[1]), "4.10.1", "x64", 62)


@needs_openssl
def test_crx3_without_signed_header_is_rejected(mod, keys, appid):
    with pytest.raises(RuntimeError, match="without signed header"):
        mod.unpack(crx3(make_zip(), keys[0], no_signed=True), "4.10.1", "x64", 62)


def test_signed_header_without_a_16_byte_id_is_rejected(mod, appid):
    header = field(10000, field(1, b"short"))
    with pytest.raises(RuntimeError, match="without its ID"):
        mod.verify_crx3(header, b"")


@pytest.mark.parametrize("data", [
    b"",
    b"Cr24",
    b"Cr24" + struct.pack("<II", 2, 0) + b"x",       # CRX2
    b"PK\x03\x04" + struct.pack("<II", 3, 0) + b"x",  # a bare ZIP
    b"Cr24" + struct.pack("<II", 3, 100) + b"x" * 50,  # header longer than the file
    b"Cr24" + struct.pack("<II", 3, 4) + b"abcd",     # nothing after the header
])
def test_not_a_crx3(mod, data):
    with pytest.raises((RuntimeError, struct.error)):
        mod.unpack(data, "4.10.1", "x64", 62)


@needs_openssl
@pytest.mark.parametrize("kwargs, version, machine, error", [
    ({"manifest": json.dumps({"name": "Other", "version": "4.10.1"})}, "4.10.1", 62, RuntimeError),
    ({}, "4.10.2", 62, RuntimeError),                          # server named another version
    ({"manifest": "not json"}, "4.10.1", 62, ValueError),
    ({"arch": "x64", "machine": 183}, "4.10.1", 62, RuntimeError),  # arm64 library in x64 dir
    ({"skip": ("LICENSE",)}, "4.10.1", 62, KeyError),
    ({"license_size": 64 * 1024 + 1}, "4.10.1", 62, RuntimeError),
])
def test_unpack_rejects_wrong_contents(mod, keys, appid, kwargs, version, machine, error):
    data = crx3(make_zip(**kwargs), keys[0])
    with pytest.raises(error):
        mod.unpack(data, version, "x64", machine)


@needs_openssl
def test_unpack_rejects_a_non_elf_library(mod, keys, appid):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"name": "WidevineCdm", "version": "4.10.1"}))
        z.writestr("LICENSE", "L")
        z.writestr("_platform_specific/linux_x64/libwidevinecdm.so", "#!/bin/sh\nrm -rf ~\n" * 4)
    with pytest.raises(RuntimeError, match="no library"):
        mod.unpack(crx3(buf.getvalue(), keys[0]), "4.10.1", "x64", 62)


# --- Update server answer and download -------------------------------------------------------

class Response(io.BytesIO):
    pass


class FakeNet:
    def __init__(self):
        self.answers, self.calls = {}, []

    def __call__(self, req, timeout=None):
        assert req.get_header("User-agent") == "Plinius Arch Linux"
        url = req.full_url
        self.calls.append((url, req.data))
        answer = self.answers.get(url, urllib.error.URLError("unreachable"))
        if isinstance(answer, BaseException):
            raise answer
        return Response(answer)


@pytest.fixture
def net(mod, monkeypatch):
    n = FakeNet()
    monkeypatch.setattr(mod.urllib.request, "urlopen", n)
    return n


def server_answer(status="ok", version="4.10.2830.0", name="cdm.crx3", digest="ab" * 32,
                  size=1000, codebases=("https://dl.google.com/x/",)):
    check = {"status": status}
    if status == "ok":
        check["manifest"] = {"version": version, "packages": {"package": [
            {"name": name, "hash_sha256": digest, "size": size}]}}
        check["urls"] = {"url": [{"codebase": c} for c in codebases]}
    return (")]}'\n" + json.dumps({"response": {"app": [{"updatecheck": check}]}})).encode()


def test_ask_server_parses_an_offer(mod, net):
    net.answers[mod.UPDATE_URL] = server_answer(digest="AB" * 32,
                                                codebases=("http://insecure/", "https://dl/"))
    assert mod.ask_server("4.10.1.0", "x64") == ("4.10.2830.0", "cdm.crx3", "ab" * 32, 1000,
                                                 ["https://dl/"])
    body = json.loads(net.calls[0][1])
    assert body["request"]["app"][0] == {"appid": mod.APPID, "updatecheck": {}, "version": "4.10.1.0"}
    assert body["request"]["arch"] == "x64"


def test_ask_server_noupdate_and_first_install(mod, net):
    net.answers[mod.UPDATE_URL] = server_answer(status="noupdate")
    assert mod.ask_server(None, "x64") is None
    assert json.loads(net.calls[0][1])["request"]["app"][0]["version"] == "0.0.0.0"


@pytest.mark.parametrize("kwargs, message", [
    ({"status": "error-unknownApplication"}, "status"),
    ({"version": "4.10"}, "odd version"),
    ({"version": "../../../etc"}, "odd version"),
    ({"name": "../cdm.crx3"}, "odd package"),
    ({"name": "cdm.zip"}, "odd package"),
    ({"digest": "ab" * 31}, "odd package"),
    ({"digest": "zz" * 32}, "odd package"),
    ({"size": 0}, "odd size"),
    ({"size": 100 * 1024 * 1024 + 1}, "odd size"),
    ({"codebases": ("http://dl.google.com/",)}, "no HTTPS"),
    ({"codebases": ()}, "no HTTPS"),
])
def test_ask_server_rejects_odd_answers(mod, net, kwargs, message):
    net.answers[mod.UPDATE_URL] = server_answer(**kwargs)
    with pytest.raises(RuntimeError, match=message):
        mod.ask_server("4.10.1.0", "x64")


def test_ask_server_without_json_raises(mod, net):
    net.answers[mod.UPDATE_URL] = b"<html>captive portal</html>"
    with pytest.raises(ValueError):
        mod.ask_server(None, "x64")


def test_download_checks_size_and_sha256_and_tries_the_next_base(mod, net):
    data = b"crx" * 100
    digest = hashlib.sha256(data).hexdigest()
    net.answers["https://a/cdm.crx3"] = data[:-1]                  # truncated
    net.answers["https://b/cdm.crx3"] = data[:-1] + b"X"           # same size, other content
    net.answers["https://d/cdm.crx3"] = data
    bases = ["https://a", "https://b/", "https://c/", "https://d/"]  # c: unreachable
    assert mod.download(bases, "cdm.crx3", digest, len(data)) == data
    with pytest.raises(RuntimeError, match="download failed") as e:
        mod.download(bases[:3], "cdm.crx3", digest, len(data))
    assert "SHA-256" in str(e.value) and "unreachable" in str(e.value)


# --- Installed layout ------------------------------------------------------------------------

FILES = {"libwidevinecdm.so": b"lib", "manifest.json": b"{}", "LICENSE": b"L"}


def test_install_links_and_installed_version(mod, state):
    assert mod.installed_version() is None
    mod.install("4.10.1.0", FILES)
    mod.ensure_gmp_links()
    assert os.readlink(state / "current") == "versions/4.10.1.0"
    assert mod.installed_version() == "4.10.1.0"
    lib = state / "gmp-widevinecdm" / "system-installed" / "libwidevinecdm.so"
    assert lib.read_bytes() == b"lib"
    assert (state / "versions" / "4.10.1.0" / "libwidevinecdm.so").stat().st_mode & 0o777 == 0o755
    assert (state / "versions" / "4.10.1.0" / "LICENSE").stat().st_mode & 0o777 == 0o644
    mod.install("4.10.2.0", {**FILES, "libwidevinecdm.so": b"lib2"})
    assert lib.read_bytes() == b"lib2"
    assert not list((state / "versions").glob(".*"))


def test_ensure_gmp_links_repairs_wrong_entries(mod, state):
    folder = state / "gmp-widevinecdm" / "system-installed"
    folder.mkdir(parents=True)
    (folder / "libwidevinecdm.so").write_bytes(b"planted")
    (folder / "manifest.json").symlink_to("/etc/passwd")
    mod.ensure_gmp_links()
    assert os.readlink(folder / "libwidevinecdm.so") == "../../current/libwidevinecdm.so"
    assert os.readlink(folder / "manifest.json") == "../../current/manifest.json"


@pytest.mark.parametrize("target", ["versions/../../etc", "/usr/lib", "versions/4.10.1.0.bad",
                                    "versions/9.9.9.9"])
def test_installed_version_rejects_odd_links(mod, state, target):
    mod.install("4.10.1.0", FILES)
    (state / "current").unlink()
    (state / "current").symlink_to(target)
    assert mod.installed_version() is None


def test_prune_keeps_the_newest_two_and_the_current(mod, state):
    for v in ("1.0.0.1", "1.0.0.10", "1.0.0.9", "0.9.0.0"):
        (state / "versions" / v).mkdir(parents=True)
    (state / "versions" / ".2.0.0.0.new").mkdir()
    (state / "versions" / "notes").mkdir()
    mod.prune("0.9.0.0")
    assert sorted(p.name for p in (state / "versions").iterdir()) == [
        "0.9.0.0", "1.0.0.10", "1.0.0.9", "notes"]


def test_version_key_orders_numerically(mod):
    assert mod.version_key("4.10.2830.0") > mod.version_key("4.10.999.0")
    assert mod.version_key("4.10.1.0") < mod.version_key("4.10.1.1")


# --- main (x64 path): no downgrade, no reinstall ---------------------------------------------

@pytest.fixture
def x64(mod, monkeypatch):
    real = os.uname()

    class Uname:
        machine = "x86_64"
        release = real.release
    monkeypatch.setattr(mod.os, "uname", lambda: Uname)


def test_main_does_not_downgrade_or_reinstall(mod, state, x64, monkeypatch):
    mod.install("4.10.5.0", FILES)
    for offered in ("4.10.5.0", "4.10.4.0"):
        monkeypatch.setattr(mod, "ask_server", lambda current, arch, v=offered:
                            (v, "c.crx3", "0" * 64, 1, ["https://x/"]))
        monkeypatch.setattr(mod, "download", lambda *a: pytest.fail("must not download"))
        assert mod.main() == 0
        assert mod.installed_version() == "4.10.5.0"


def test_main_installs_a_newer_offer(mod, state, x64, monkeypatch):
    mod.install("4.10.5.0", FILES)
    monkeypatch.setattr(mod, "ask_server", lambda current, arch:
                        ("4.10.6.0", "c.crx3", "0" * 64, 1, ["https://x/"]))
    monkeypatch.setattr(mod, "download", lambda *a: b"crx")
    monkeypatch.setattr(mod, "unpack", lambda data, version, arch, machine:
                        {**FILES, "libwidevinecdm.so": b"new"} if (arch, machine) == ("x64", 62)
                        else pytest.fail("wrong arch"))
    assert mod.main() == 0
    assert mod.installed_version() == "4.10.6.0"
    assert (state / "gmp-widevinecdm" / "system-installed" / "libwidevinecdm.so").read_bytes() == b"new"


def test_main_failing_verification_keeps_the_installed_cdm(mod, state, x64, monkeypatch):
    mod.install("4.10.5.0", FILES)
    monkeypatch.setattr(mod, "ask_server", lambda current, arch:
                        ("4.10.6.0", "c.crx3", "0" * 64, 1, ["https://x/"]))
    monkeypatch.setattr(mod, "download", lambda *a: b"Cr24" + b"\x00" * 20)
    with pytest.raises((RuntimeError, struct.error)):
        mod.main()
    assert mod.installed_version() == "4.10.5.0"
    assert sorted(p.name for p in (state / "versions").iterdir()) == ["4.10.5.0"]


def test_main_without_offer_and_without_cdm_fails(mod, state, x64, monkeypatch):
    monkeypatch.setattr(mod, "ask_server", lambda current, arch: None)
    with pytest.raises(RuntimeError, match="offers no Widevine"):
        mod.main()


# --- ChromeOS CDM (arm64) --------------------------------------------------------------------

CROS = {"libwidevinecdm.so": b"fixed lib", "LICENSE": b"license",
        "manifest.json": json.dumps({"version": "4.10.2662.3"}).encode()}


class Stream(io.BytesIO):
    pass


@pytest.fixture
def cros(mod, state, monkeypatch):
    """The LaCrOS download and its tools faked: urlopen yields `image`, unsquashfs and the
    fixup script write the CROS files (or what the test puts in `files`)."""
    setup = {"image": b"i" * 1000, "files": dict(CROS), "calls": []}
    monkeypatch.setattr(mod, "LACROS_SIZE", 1000)
    monkeypatch.setattr(mod, "FIXED_FILES", {k: hashlib.sha256(v).hexdigest() for k, v in CROS.items()})
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda req, timeout=None: Stream(setup["image"]) if req.full_url == mod.LACROS_URL
                        and req.get_header("User-agent") == "Plinius Arch Linux" else None)

    def run(cmd, **kwargs):
        setup["calls"].append(cmd)
        if cmd[0] == "unsquashfs":
            root = cmd[cmd.index("-d") + 1]
            cdm = os.path.join(root, "WidevineCdm")
            os.makedirs(os.path.join(cdm, "_platform_specific", "cros_arm64"))
            for name in ("manifest.json", "LICENSE"):
                with open(os.path.join(cdm, name), "wb") as f:
                    f.write(setup["files"][name])
            with open(os.path.join(cdm, "_platform_specific", "cros_arm64", "libwidevinecdm.so"), "wb") as f:
                f.write(b"unfixed")
        elif cmd[0] == sys.executable:
            with open(cmd[-1], "wb") as f:
                f.write(setup["files"]["libwidevinecdm.so"])
        else:
            pytest.fail(f"unexpected command {cmd}")
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(mod.subprocess, "run", run)
    return setup


def test_main_cros_installs_the_known_files(mod, state, cros):
    assert mod.main_cros() == 0
    assert mod.installed_version() == mod.CROS_VERSION
    assert (state / "current" / "libwidevinecdm.so").read_bytes() == b"fixed lib"
    assert not (state / ".work").exists()
    # Already there and unchanged: nothing is downloaded again
    cros["calls"].clear()
    assert mod.main_cros() == 0
    assert cros["calls"] == []


def test_main_cros_reinstalls_a_damaged_copy(mod, state, cros):
    mod.main_cros()
    (state / "versions" / mod.CROS_VERSION / "libwidevinecdm.so").write_bytes(b"changed")
    cros["calls"].clear()
    mod.main_cros()
    assert len(cros["calls"]) == 2
    assert (state / "current" / "libwidevinecdm.so").read_bytes() == b"fixed lib"


@pytest.mark.parametrize("image, message", [
    (b"i" * 999, "has 999 bytes"),
    (b"i" * 1001, "larger than expected"),
    (b"", "has 0 bytes"),
])
def test_fetch_cros_rejects_a_wrong_image_size(mod, state, cros, image, message):
    cros["image"] = image
    with pytest.raises(RuntimeError, match=message):
        mod.main_cros()
    assert mod.installed_version() is None
    assert not (state / ".work").exists()


@pytest.mark.parametrize("name", ["libwidevinecdm.so", "LICENSE", "manifest.json"])
def test_fetch_cros_rejects_unknown_files(mod, state, cros, name):
    cros["files"][name] = cros["files"][name] + b" "
    with pytest.raises(RuntimeError, match="not the known one"):
        mod.main_cros()
    assert mod.installed_version() is None


def test_fetch_cros_checks_the_manifest_version(mod, state, cros, monkeypatch):
    other = json.dumps({"version": "1.0.0.0"}).encode()
    cros["files"]["manifest.json"] = other
    monkeypatch.setattr(mod, "FIXED_FILES", {**mod.FIXED_FILES,
                                             "manifest.json": hashlib.sha256(other).hexdigest()})
    with pytest.raises(RuntimeError, match="expected version"):
        mod.main_cros()


def test_fixed_files_are_real_digests(mod):
    assert set(mod.FIXED_FILES) == {"libwidevinecdm.so", "manifest.json", "LICENSE"}
    for digest in mod.FIXED_FILES.values():
        assert len(digest) == 64 and int(digest, 16) >= 0


# --- plinius-widevine-set and the profile.d snippet ------------------------------------------

@pytest.fixture
def setter(tmp_path, fake_bin):
    """plinius-widevine-set as a copy whose marker and state point into tmp_path."""
    text = (DIR / "plinius-widevine-set").read_text()
    for old in ("/etc/plinius-widevine/enabled", "/var/lib/plinius-widevine"):
        assert old in text
    text = (text.replace("/etc/plinius-widevine/enabled", str(tmp_path / "etc" / "enabled"))
                .replace("/var/lib/plinius-widevine", str(tmp_path / "state")))
    script = tmp_path / "plinius-widevine-set"
    script.write_text(text)
    fake_bin.stub("systemctl", '[ "$1" = start ] && exit "${START_RC:-0}"; exit 0')

    def run(*args, **env):
        return subprocess.run(["sh", str(script), *args], env=fake_bin.env(isolated=True, **env),
                              capture_output=True, text=True)
    return run


def test_set_on_writes_the_marker_and_starts_the_download(setter, tmp_path, fake_bin):
    assert setter("on").returncode == 0
    assert (tmp_path / "etc" / "enabled").exists()
    assert fake_bin.calls("systemctl") == [["start", "plinius-widevine-update.service"]]


def test_set_on_failed_download_exits_3_and_keeps_the_marker(setter, tmp_path):
    assert setter("on", START_RC=1).returncode == 3
    assert (tmp_path / "etc" / "enabled").exists()


def test_set_off_removes_marker_and_cdm_only(setter, tmp_path, fake_bin):
    state = tmp_path / "state"
    for d in ("versions/1.0.0.0", "gmp-widevinecdm/system-installed", ".work"):
        (state / d).mkdir(parents=True)
    (state / "current").symlink_to("versions/1.0.0.0")
    (state / "keep").write_text("x")
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "enabled").write_text("")
    assert setter("off").returncode == 0
    assert not (tmp_path / "etc" / "enabled").exists()
    assert sorted(p.name for p in state.iterdir()) == ["keep"]
    assert ["stop", "plinius-widevine-update.service"] in fake_bin.calls("systemctl")


@pytest.mark.parametrize("args", [[], ["enable"], ["ON"]])
def test_set_usage(setter, tmp_path, fake_bin, args):
    r = setter(*args)
    assert r.returncode == 2 and "Usage" in r.stderr
    assert fake_bin.calls("systemctl") == []
    assert not (tmp_path / "etc").exists()


GMP = "/var/lib/plinius-widevine/gmp-widevinecdm/system-installed"


@pytest.mark.parametrize("before, after", [
    (None, GMP),
    ("", GMP),
    ("/opt/gmp", "/opt/gmp:" + GMP),
    (GMP, GMP),
    ("/a:" + GMP + ":/b", "/a:" + GMP + ":/b"),
    (GMP + "-old", GMP + "-old:" + GMP),
])
def test_profile_snippet_adds_the_path_once(before, after):
    env = {"PATH": os.environ["PATH"]}
    if before is not None:
        env["MOZ_GMP_PATH"] = before
    r = subprocess.run(["sh", "-c", '. "$1"; . "$1"; printf %s "$MOZ_GMP_PATH"', "sh",
                        str(DIR / "plinius-widevine.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == after


# --- Odd but harmless header fields, leftovers, the architectures, the program ---------------

@needs_openssl
def test_crx3_header_fields_it_does_not_know_are_skipped(mod, keys, appid):
    archive = make_zip()
    good = crx3(archive, keys[0])
    magic, version, length = struct.unpack_from("<4sII", good)
    # A proof without a signature, a varint field 10000 and an unknown field before the rest
    extra = field(2, field(1, keys[1].der)) + field(10000, 5) + field(3, b"future")
    header = extra + good[12:12 + length]
    data = magic + struct.pack("<II", version, len(header)) + header + good[12 + length:]
    files = mod.unpack(data, "4.10.1", "x64", 62)
    assert files["libwidevinecdm.so"] == elf(62)


def test_install_replaces_a_leftover_link(mod, state):
    mod.install("4.10.1.0", FILES)
    # An earlier run died between making the new link and putting it in place
    (state / ".current.new").symlink_to("versions/0.0.0.0")
    mod.install("4.10.2.0", FILES)
    assert os.readlink(state / "current") == "versions/4.10.2.0"
    assert not (state / ".current.new").is_symlink()


def test_prune_without_any_version(mod, state):
    mod.prune("4.10.1.0")
    assert list(state.iterdir()) == []


def test_main_cros_reinstalls_when_a_file_is_missing(mod, state, cros):
    mod.main_cros()
    (state / "versions" / mod.CROS_VERSION / "LICENSE").unlink()
    assert mod.cros_installed() is False
    cros["calls"].clear()
    mod.main_cros()
    assert len(cros["calls"]) == 2 and mod.cros_installed() is True


def test_fetch_cros_only_on_4k_pages(mod, state, cros, monkeypatch):
    monkeypatch.setattr(mod.os, "sysconf", lambda name: 16384)
    with pytest.raises(RuntimeError, match="4 KiB pages"):
        mod.main_cros()
    assert cros["calls"] == [] and mod.installed_version() is None


REAL_RELEASE = os.uname().release


def uname(machine):
    return type("Uname", (), {"machine": machine, "release": REAL_RELEASE})


def test_main_on_another_architecture(mod, state, monkeypatch, capsys):
    monkeypatch.setattr(mod.os, "uname", lambda: uname("riscv64"))
    monkeypatch.setattr(mod, "ask_server", lambda *a: pytest.fail("nothing to ask for"))
    assert mod.main() == 1
    assert "Google offers no Widevine CDM for riscv64." in capsys.readouterr().out


def test_main_on_arm64_takes_the_chromeos_cdm(mod, state, monkeypatch):
    monkeypatch.setattr(mod.os, "uname", lambda: uname("aarch64"))
    monkeypatch.setattr(mod, "ask_server", lambda *a: pytest.fail("not the update server"))
    monkeypatch.setattr(mod, "main_cros", lambda: 0)
    assert mod.main() == 0


@pytest.mark.parametrize("machine, page, code, message", [
    ("riscv64", 4096, 1, "Google offers no Widevine CDM for riscv64."),
    ("aarch64", 16384, 1, "plinius-widevine-update: the ChromeOS CDM is only known to work "
                          "with 4 KiB pages"),
])
def test_as_a_program(tmp_path, monkeypatch, capsys, machine, page, code, message):
    import runpy
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    monkeypatch.setattr(os, "uname", lambda: uname(machine))
    monkeypatch.setattr(os, "sysconf", lambda name: page)
    with pytest.raises(SystemExit) as exit_:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exit_.value.code == code
    assert message in capsys.readouterr().out
    # Nothing was left behind in the state directory
    assert list(tmp_path.iterdir()) == []
