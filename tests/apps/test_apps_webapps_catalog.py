# plinius-webapps-catalog: the catalog of web apps behind Settings > Apps > Web Apps > Add >
# Search. Checking entries (strictly for publishing, leniently on the phone), the phone's
# language and country from the locale variables, which entries and variants apply, the signed
# copy from the server against the kept and the bundled one, and the command line Settings runs.
#
# The script is imported from the repository with its cache and keyring paths pointed into
# tmp_path; the network is a fake; the signature tests use throw-away keys (apps_gpgkeys).

import io
import json
import types
import urllib.error
from pathlib import Path

import pytest

from apps_gpgkeys import gpg_keys  # noqa: F401  (fixture)
from conftest import PKG, load_script

DIR = PKG / "plinius-webapps"
SCRIPT = DIR / "plinius-webapps-catalog"


@pytest.fixture(scope="module")
def c():
    return load_script(SCRIPT)


@pytest.fixture
def home(c, tmp_path, monkeypatch):
    h = types.SimpleNamespace(cache=tmp_path / "cache", keys=tmp_path / "keys", icons=tmp_path / "icons")
    monkeypatch.setattr(c, "CACHE", h.cache)
    monkeypatch.setattr(c, "ICONS", h.icons)
    monkeypatch.setattr(c, "BUNDLED", DIR / "catalog.json")
    monkeypatch.setattr(c, "KEYRING", str(h.keys / "plinius.gpg"))
    monkeypatch.setattr(c, "TRUSTED", str(h.keys / "plinius-trusted"))
    monkeypatch.setattr(c, "REVOKED", str(h.keys / "plinius-revoked"))

    def no_network(*_a, **_kw):
        raise AssertionError("network access in a test")
    monkeypatch.setattr(c.urllib.request, "urlopen", no_network)
    return h


@pytest.fixture
def keyring(c, home, gpg_keys):
    keys, trusted, attacker = gpg_keys
    home.keys.mkdir()
    Path(c.KEYRING).write_bytes(keys.export(trusted, attacker))
    Path(c.TRUSTED).write_text(f"# trusted\n{trusted}:4:\n")
    Path(c.REVOKED).write_text("")
    return types.SimpleNamespace(keys=keys, trusted=trusted, attacker=attacker)


class Log(list):
    def __call__(self, text):
        self.append(text)

    def has(self, part):
        return any(part in line for line in self)


@pytest.fixture
def log():
    return Log()


def app(id="shop", **more):
    a = {"id": id, "name": "Shop", "url": "https://shop.example/", "category": "shopping"}
    a.update(more)
    return a


def catalog(updated=None, apps=()):
    data = {"schema": 1, "apps": list(apps)}
    if updated is not None:
        data["updated"] = updated
    return json.dumps(data).encode()


# ----- the catalog in the package -----

def test_bundled_catalog_is_valid_and_complete(c):
    data = c.parse((DIR / "catalog.json").read_bytes(), strict=True)
    assert data["updated"] == 0  # stamped only when published
    assert len(data["apps"]) >= 60
    used = {a["category"] for a in data["apps"]}
    assert used == set(c.CATEGORIES)
    # Something for each language Settings is translated to, and international entries
    for lang in ("de", "es", "fr", "it", "nl", "pl", "pt", "ru", "en"):
        local = [a for a in data["apps"]
                 if lang in a.get("languages", []) or any(lang in v.get("languages", []) for v in a["variants"])]
        assert local, lang
    assert sum("countries" not in a and "languages" not in a for a in data["apps"]) >= 30


def test_main_check(c, tmp_path, capsys):
    assert c.main(["x", "--check", str(DIR / "catalog.json")]) == 0
    assert capsys.readouterr().out.endswith(" web apps\n")
    bad = tmp_path / "bad.json"
    bad.write_bytes(catalog(apps=[app(), app()]))
    assert c.main(["x", "--check", str(bad)]) == 1
    assert "shop: id repeated" in capsys.readouterr().err
    assert c.main(["x", "--check", str(tmp_path / "missing.json")]) == 1


@pytest.mark.parametrize("args", [["--fetch"], ["--check"], ["--offline", "--offline"]])
def test_main_usage(c, args, capsys):
    assert c.main(["x", *args]) == 2
    assert capsys.readouterr().err.startswith("usage:")


# ----- checking entries -----

@pytest.mark.parametrize("entry, error", [
    ("not an object", "an entry is an object"),
    (app(id="Shop"), "id 'Shop': lower case"),
    (app(id=""), "id '': lower case"),
    (app(id="-x"), "id '-x'"),
    (app(id="a" * 41), "id"),
    (app(colour="red"), "shop: unknown keys colour"),
    ({"id": "shop", "url": "https://x.example/", "category": "news"}, "name and url are needed"),
    (app(category="weather"), "category is one of"),
    (app(name=""), "name: one line"),
    (app(name="   "), "name: one line"),
    (app(name="a\nb"), "name: one line"),
    (app(name="x" * 61), "at most 60"),
    (app(name=5), "name: one line"),
    (app(url="http://shop.example/"), "url: an https:// address"),
    (app(url="https://"), "url: an https:// address"),
    (app(url="https://shop example/"), "url: an https:// address"),
    (app(url="https:///path"), "with a host"),
    (app(url="https://user@shop.example/"), "with a host"),
    (app(keywords="shop"), "keywords: a list"),
    (app(keywords=["k"] * 21), "keywords: a list of at most 20"),
    (app(keywords=["x" * 41]), "keyword: one line"),
    (app(keywords=[3]), "keyword: one line"),
    (app(countries="DE"), "countries: a list of codes like DE"),
    (app(countries=[]), "countries: a list"),
    (app(countries=["de"]), "countries: a list"),
    (app(languages=["DE"]), "languages: a list of codes like de"),
    (app(settings=[]), "settings: only"),
    (app(settings={"zoom": 2}), "settings: only clear_cache, clear_cookies, frameless, identify_as"),
    (app(settings={"frameless": "yes"}), "settings: frameless is true or false"),
    (app(settings={"identify_as": "spaceship"}), "identify_as is one of"),
    (app(variants={}), "variants: a list"),
    (app(icon="abc"), "icon: the SHA-256"),
    (app(icon="A" * 64), "icon: the SHA-256"),
    (app(icon=5), "icon: the SHA-256"),
    (app(variants=[3]), "a variant has only"),
    (app(variants=[{"countries": ["DE"], "category": "news"}]), "a variant has only"),
    (app(variants=[{"url": "https://shop.example/de"}]), "a variant names countries or languages"),
    (app(variants=[{"countries": ["DE"], "url": "ftp://x"}]), "url: an https:// address"),
    (app(countries=["AT"], variants=[{"countries": ["DE"], "url": "https://shop.de/"}]),
     "a variant's countries are among the entry's"),
    (app(languages=["de"], variants=[{"languages": ["fr"], "name": "Boutique"}]),
     "a variant's languages are among the entry's"),
])
def test_check_app_refuses(c, entry, error):
    with pytest.raises(c.CatalogError) as e:
        c.check_app(entry)
    assert error in str(e.value)


def test_check_app_strips_and_keeps(c):
    out = c.check_app(app(name=" Shop ", keywords=[" buy "], countries=["DE", "AT"], languages=["de"],
                          settings={"frameless": False, "identify_as": "android"},
                          variants=[{"countries": ["AT"], "languages": ["de"], "url": "https://shop.at/"}]))
    assert out == {"id": "shop", "category": "shopping", "name": "Shop", "url": "https://shop.example/",
                   "keywords": ["buy"], "settings": {"frameless": False, "identify_as": "android"},
                   "countries": ["DE", "AT"], "languages": ["de"],
                   "variants": [{"url": "https://shop.at/", "countries": ["AT"], "languages": ["de"]}]}


@pytest.mark.parametrize("data", [b"{", b"[]", b'{"schema": 2, "apps": []}', b'{"schema": 1, "apps": {}}',
                                  b'{"schema": 1, "apps": [], "updated": "soon"}',
                                  b'{"schema": 1, "apps": [], "updated": -1}',
                                  b'{"schema": 1, "apps": [], "updated": true}'])
def test_parse_refuses_other_catalogs(c, data):
    with pytest.raises(c.CatalogError):
        c.parse(data)


def test_parse_leaves_out_bad_entries_unless_strict(c, capsys):
    data = catalog(5, [app("a"), app("b", url="http://x.example/"), app("a"), app("c")])
    out = c.parse(data)
    assert out["updated"] == 5 and [a["id"] for a in out["apps"]] == ["a", "c"]
    err = capsys.readouterr().err
    assert "entry left out: b: url" in err and "entry left out: a: id repeated" in err
    with pytest.raises(c.CatalogError, match="b: url"):
        c.parse(data, strict=True)
    with pytest.raises(c.CatalogError, match="unknown keys at the top"):
        c.parse(json.dumps({"schema": 1, "apps": [], "note": "x"}).encode(), strict=True)
    # Lenient on the phone: an unknown key at the top of a newer catalog is no reason to refuse it
    assert c.parse(json.dumps({"schema": 1, "apps": [], "note": "x"}).encode())["apps"] == []


# ----- the phone's language and country -----

@pytest.mark.parametrize("env, expected", [
    ({"LANG": "de_DE.UTF-8"}, ("de", "DE")),
    ({"LANG": "de_AT.UTF-8@euro"}, ("de", "AT")),
    ({"LANG": "en_US.UTF-8", "LC_MONETARY": "de_CH.UTF-8"}, ("en", "CH")),
    ({"LANG": "en_US.UTF-8", "LC_TIME": "fr_BE.UTF-8", "LC_PAPER": "nl_NL.UTF-8"}, ("en", "NL")),
    ({"LANG": "en_US.UTF-8", "LC_MESSAGES": "pl_PL.UTF-8"}, ("pl", "US")),
    ({"LANG": "de_DE.UTF-8", "LANGUAGE": "fr:en"}, ("fr", "DE")),
    ({"LANG": "de_DE.UTF-8", "LC_ALL": "it_IT.UTF-8", "LC_MONETARY": "de_CH.UTF-8"}, ("it", "IT")),
    ({"LANG": "C.UTF-8"}, (None, None)),
    ({"LANG": "POSIX"}, (None, None)),
    ({"LANG": "eo"}, ("eo", None)),
    ({"LANG": "C.UTF-8", "LC_NUMERIC": "pt_BR.UTF-8"}, (None, "BR")),
    ({}, (None, None)),
])
def test_phone_locale(c, env, expected):
    assert c.phone_locale(env) == expected


def test_phone_locale_reads_the_environment(c, monkeypatch):
    for var in ("LC_ALL", "LANGUAGE", "LC_MESSAGES", "LC_MONETARY", "LC_PAPER", "LC_MEASUREMENT", "LC_TIME",
                "LC_NUMERIC"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "es_ES.UTF-8")
    assert c.phone_locale() == ("es", "ES")


# ----- which entries, with which variants -----

SAMPLE = [
    app("global", name="Global", url="https://global.example/", category="news"),
    app("shop", name="Shop", url="https://shop.com/", keywords=["buy"],
        settings={"clear_cookies": True},
        variants=[{"countries": ["DE", "AT"], "url": "https://shop.de/"},
                  {"languages": ["de"], "name": "Laden", "keywords": ["kaufen"]},
                  {"countries": ["AT"], "languages": ["de"], "url": "https://shop.at/",
                   "settings": {"identify_as": "android"}},
                  {"languages": ["fr"], "name": "Boutique", "settings": {"clear_cookies": False}}]),
    app("de-only", name="Bahn", url="https://bahn.example/", category="travel", countries=["DE"]),
    app("german", name="Zeitung", url="https://zeitung.example/", category="news", languages=["de"]),
    app("swiss-french", name="Info", url="https://info.example/", category="news", countries=["CH"],
        languages=["fr"]),
    app("aaa", name="aaa", url="https://aaa.example/", category="news"),
]


@pytest.fixture(autouse=True)
def no_icons(c, tmp_path, monkeypatch):
    """No icon of the system is ever looked at."""
    monkeypatch.setattr(c, "ICONS", tmp_path / "no-icons")
    monkeypatch.setattr(c, "CACHE", tmp_path / "no-cache")


def resolved(c, language, country):
    data = c.parse(catalog(1, SAMPLE))
    return {a["id"]: a for a in c.resolve(data, language, country)}


def test_resolve_filters_by_country_and_language(c):
    assert set(resolved(c, "de", "DE")) == {"global", "shop", "de-only", "german", "aaa"}
    assert set(resolved(c, "en", "DE")) == {"global", "shop", "de-only", "aaa"}
    assert set(resolved(c, "de", "CH")) == {"global", "shop", "german", "aaa"}
    assert set(resolved(c, "fr", "CH")) == {"global", "shop", "swiss-french", "aaa"}
    assert set(resolved(c, "fr", "FR")) == {"global", "shop", "aaa"}
    assert set(resolved(c, None, None)) == {"global", "shop", "aaa"}


def test_resolve_applies_variants_from_the_least_specific(c):
    plain = resolved(c, "en", "US")["shop"]
    assert plain == {"id": "shop", "name": "Shop", "url": "https://shop.com/", "category": "shopping",
                     "keywords": ["buy"], "frameless": True, "clear_cache": False, "clear_cookies": True,
                     "identify_as": "", "icon": ""}
    # Country: the address; language: name and keywords
    de = resolved(c, "de", "DE")["shop"]
    assert (de["name"], de["url"], de["keywords"], de["identify_as"]) == ("Laden", "https://shop.de/", ["kaufen"], "")
    # Country and language together win over the country alone, settings key by key
    at = resolved(c, "de", "AT")["shop"]
    assert (at["name"], at["url"], at["identify_as"], at["clear_cookies"]) == (
        "Laden", "https://shop.at/", "android", True)
    # English speaker in Austria: only the country's variant
    assert resolved(c, "en", "AT")["shop"]["url"] == "https://shop.de/"
    fr = resolved(c, "fr", "FR")["shop"]
    assert (fr["name"], fr["clear_cookies"]) == ("Boutique", False)


def test_resolve_orders_by_category_then_name(c):
    data = c.parse(catalog(1, SAMPLE))
    assert [a["id"] for a in c.resolve(data, "de", "DE")] == ["aaa", "global", "german", "shop", "de-only"]


def test_resolve_does_not_change_the_catalog(c):
    data = c.parse(catalog(1, SAMPLE))
    before = json.dumps(data, sort_keys=True)
    c.resolve(data, "de", "AT")
    assert json.dumps(data, sort_keys=True) == before


# ----- the signature -----

def test_verify(c, keyring):
    data = catalog(5)
    good = keyring.keys.sign(keyring.trusted, data)
    assert c.verify(data, good)
    assert not c.verify(data + b" ", good)
    assert not c.verify(data, b"garbage")
    assert not c.verify(data, keyring.keys.sign(keyring.attacker, data))
    Path(c.REVOKED).write_text(f"{keyring.trusted}\n")
    assert not c.verify(data, good)


def test_verify_without_trust_keyring_or_gpgv(c, keyring, monkeypatch):
    data = catalog(5)
    good = keyring.keys.sign(keyring.trusted, data)
    Path(c.KEYRING).write_text("not armored\n")
    assert not c.verify(data, good)
    Path(c.KEYRING).unlink()
    assert not c.verify(data, good)
    Path(c.KEYRING).write_bytes(keyring.keys.export(keyring.trusted))

    def no_gpgv(*_a, **_kw):
        raise FileNotFoundError("gpgv")
    monkeypatch.setattr(c.subprocess, "run", no_gpgv)
    assert not c.verify(data, good)
    Path(c.TRUSTED).unlink()
    assert not c.verify(data, good)


def test_dearmor(c):
    import base64
    body = base64.b64encode(b"\x99keydata").decode()
    text = (f"-----BEGIN PGP PUBLIC KEY BLOCK-----\nComment: x\n\n{body}\n=abcd\n"
            "-----END PGP PUBLIC KEY BLOCK-----\n")
    assert c._dearmor(text) == b"\x99keydata"
    bare = f"-----BEGIN PGP PUBLIC KEY BLOCK-----\n{body}\n-----END PGP PUBLIC KEY BLOCK-----"
    assert c._dearmor(bare) == b"\x99keydata"
    with pytest.raises(StopIteration):
        c._dearmor("no key here")


# ----- the server, the kept and the bundled copy -----

class Server:
    """urllib.request.urlopen for the catalog and its signature."""

    def __init__(self, files=None, error=None):
        self.files = files or {}
        self.error = error
        self.reads = []

    def __call__(self, req, timeout=None):
        assert req.get_header("User-agent") == "Plinius Arch Linux"
        url = req.full_url
        if self.error:
            raise self.error
        server = self

        class Answer(io.BytesIO):
            def read(self, n=-1):
                server.reads.append((url, n, timeout))
                return super().read(n)
        return Answer(self.files[url])


def serve(c, monkeypatch, keyring, data, signer=None, signature=None):
    sig = signature if signature is not None else keyring.keys.sign(signer or keyring.trusted, data)
    server = Server({c.URL: data, c.URL + ".sig": sig})
    monkeypatch.setattr(c.urllib.request, "urlopen", server)
    return server


def bundled(c):
    return c.parse((DIR / "catalog.json").read_bytes())


def test_load_fetches_a_newer_signed_catalog_and_keeps_it(c, home, keyring, monkeypatch, log):
    data = catalog(2_000_000_000, [app("new")])
    server = serve(c, monkeypatch, keyring, data)
    assert [a["id"] for a in c.load(log=log)["apps"]] == ["new"]
    assert (home.cache / "catalog.json").read_bytes() == data
    assert c.verify(data, (home.cache / "catalog.json.sig").read_bytes())
    # Limited reads (one byte more tells a larger answer), the short timeout
    assert [(n, t) for _u, n, t in server.reads] == [((4 << 20) + 1, 6), ((64 << 10) + 1, 6)]
    assert not list(home.cache.glob("*.tmp"))
    # Without a network the kept copy is used
    monkeypatch.setattr(c.urllib.request, "urlopen", Server(error=urllib.error.URLError("down")))
    log.clear()
    assert [a["id"] for a in c.load(log=log)["apps"]] == ["new"]
    assert log[0].startswith("not fetched")
    assert [a["id"] for a in c.load(fetch=False, log=log)["apps"]] == ["new"]


def test_load_refuses_bad_or_older_catalogs(c, home, keyring, monkeypatch, log):
    # Signed by someone else
    serve(c, monkeypatch, keyring, catalog(2_000_000_000, [app("evil")]), keyring.attacker)
    assert c.load(log=log) == bundled(c)
    assert "the fetched catalog fails the signature check, not used" in log
    assert not home.cache.exists()
    # Signed, but no catalog
    serve(c, monkeypatch, keyring, b'{"schema": 9}')
    assert c.load(log=log) == bundled(c) and log.has("the fetched catalog is not usable")
    # Larger than a catalog may be
    serve(c, monkeypatch, keyring, b" " * ((4 << 20) + 1), signature=b"x")
    assert c.load(log=log) == bundled(c) and log.has("more than 4194304 bytes")
    # A newer kept copy than the fetched one stays
    newer = catalog(2_100_000_000, [app("kept")])
    home.cache.mkdir()
    (home.cache / "catalog.json").write_bytes(newer)
    (home.cache / "catalog.json.sig").write_bytes(keyring.keys.sign(keyring.trusted, newer))
    serve(c, monkeypatch, keyring, catalog(2_000_000_000, [app("older")]))
    assert [a["id"] for a in c.load(log=log)["apps"]] == ["kept"]
    assert "the fetched catalog is older than the kept one, not used" in log
    assert (home.cache / "catalog.json").read_bytes() == newer
    # A catalog older than the bundled one loses against it
    (home.cache / "catalog.json").unlink()
    serve(c, monkeypatch, keyring, catalog(None, [app("ancient")]))
    bundled_ids = [a["id"] for a in bundled(c)["apps"]]
    # The same time as the bundled one (0): the first, the fetched one, wins
    assert [a["id"] for a in c.load(log=log)["apps"]] == ["ancient"]
    monkeypatch.setattr(c, "BUNDLED", home.cache / "bundled.json")
    (home.cache / "bundled.json").write_bytes(catalog(5, [app("packaged")]))
    assert [a["id"] for a in c.load(log=log)["apps"]] == ["packaged"]
    assert bundled_ids


def test_a_catalog_that_cannot_be_kept_is_still_used(c, home, keyring, monkeypatch, log):
    home.cache.parent.mkdir(parents=True, exist_ok=True)
    home.cache.write_text("a file where the folder should be")
    serve(c, monkeypatch, keyring, catalog(2_000_000_000, [app("new")]))
    assert [a["id"] for a in c.load(log=log)["apps"]] == ["new"]
    assert log.has("not kept")


def test_kept_copy_is_checked_again(c, home, keyring, log):
    home.cache.mkdir()
    data = catalog(2_000_000_000, [app("kept")])
    (home.cache / "catalog.json").write_bytes(data + b" ")
    (home.cache / "catalog.json.sig").write_bytes(keyring.keys.sign(keyring.trusted, data))
    assert c.load(fetch=False, log=log) == bundled(c)
    signed_junk = b"not json"
    (home.cache / "catalog.json").write_bytes(signed_junk)
    (home.cache / "catalog.json.sig").write_bytes(keyring.keys.sign(keyring.trusted, signed_junk))
    assert c._read_cache() is None


def test_load_without_any_catalog(c, home, monkeypatch, tmp_path, log):
    monkeypatch.setattr(c, "BUNDLED", tmp_path / "missing.json")
    assert c.load(fetch=False, log=log) == {"updated": 0, "apps": []}
    assert log[0].startswith("bundled catalog not readable")


def test_load_logs_to_stderr_by_default(c, home, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(c, "BUNDLED", tmp_path / "missing.json")
    c.load(fetch=False)
    assert capsys.readouterr().err.startswith("plinius-webapps-catalog: bundled catalog not readable")


# ----- what Settings reads -----

def test_main_prints_the_entries_for_the_phone(c, home, monkeypatch, capsys):
    for var in ("LC_ALL", "LANGUAGE", "LC_MESSAGES", "LC_MONETARY", "LC_PAPER", "LC_MEASUREMENT", "LC_TIME",
                "LC_NUMERIC"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    assert c.main(["x", "--offline"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["updated"], out["language"], out["country"]) == (0, "de", "DE")
    by_id = {a["id"]: a for a in out["apps"]}
    assert by_id["amazon"]["url"] == "https://www.amazon.de/"
    assert by_id["db"]["url"] == "https://www.bahn.de/"
    assert by_id["youtube"]["identify_as"] == "android"
    assert "sncf-connect" not in by_id
    assert set(by_id["gmail"]) == {"id", "name", "url", "category", "keywords", "frameless", "clear_cache",
                                   "clear_cookies", "identify_as", "icon"}
    assert by_id["gmail"]["icon"] == ""  # the test's icon folder is empty


def test_main_asks_the_server_without_offline(c, home, keyring, monkeypatch, capsys):
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    serve(c, monkeypatch, keyring, catalog(2_000_000_000, [app("served", name="Servi")]))
    assert c.main(["x"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["updated"] == 2_000_000_000 and [a["name"] for a in out["apps"]] == ["Servi"]


# ----- icons -----

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
OTHER_PNG = b"\x89PNG\r\n\x1a\n" + b"other"


def sha(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def test_check_app_keeps_the_icon_checksum(c):
    assert c.check_app(app(icon=sha(PNG)))["icon"] == sha(PNG)
    assert c.check_app(app(icon=sha(PNG)))["icon"] == c.parse(catalog(1, [app(icon=sha(PNG))]))["apps"][0]["icon"]


def test_icon_of_the_bundled_catalog_is_the_package_s(c, home):
    home.icons.mkdir()
    (home.icons / "shop.png").write_bytes(PNG)
    assert c.icon_file({"id": "shop"}) == str(home.icons / "shop.png")
    assert c.icon_file({"id": "other"}) == ""


def test_icon_with_a_checksum_only_when_it_matches(c, home):
    home.icons.mkdir()
    (home.icons / "shop.png").write_bytes(PNG)
    assert c.icon_file({"id": "shop", "icon": sha(PNG)}) == str(home.icons / "shop.png")
    # Another icon on the server: the package's no longer counts, the kept one when it matches
    assert c.icon_file({"id": "shop", "icon": sha(OTHER_PNG)}) == ""
    (home.cache / "icons").mkdir(parents=True)
    (home.cache / "icons" / "shop.png").write_bytes(OTHER_PNG)
    assert c.icon_file({"id": "shop", "icon": sha(OTHER_PNG)}) == str(home.cache / "icons" / "shop.png")
    (home.cache / "icons" / "shop.png").write_bytes(OTHER_PNG + b"x")
    assert c.icon_file({"id": "shop", "icon": sha(OTHER_PNG)}) == ""


def test_resolve_names_the_icon(c, home):
    home.icons.mkdir()
    (home.icons / "shop.png").write_bytes(PNG)
    data = c.parse(catalog(1, [app(icon=sha(PNG)), app("other", icon=sha(OTHER_PNG))]))
    icons = {a["id"]: a["icon"] for a in c.resolve(data, "de", "DE")}
    assert icons == {"shop": str(home.icons / "shop.png"), "other": ""}


def icon_server(c, monkeypatch, files):
    server = Server({c.ICON_URL.format(id=k): v for k, v in files.items()})

    def urlopen(req, timeout=None):
        if req.full_url not in server.files:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)
        return server(req, timeout)
    monkeypatch.setattr(c.urllib.request, "urlopen", urlopen)
    return server


def test_fetch_icons_brings_what_is_missing_and_checks_it(c, home, monkeypatch, log):
    home.icons.mkdir()
    (home.icons / "packaged.png").write_bytes(PNG)
    data = c.parse(catalog(1, [app("packaged", icon=sha(PNG)), app("new", icon=sha(OTHER_PNG)),
                               app("wrong", icon=sha(OTHER_PNG)), app("notpng", icon=sha(b"GIF89a")),
                               app("gone", icon=sha(PNG)), app("plain")]))
    server = icon_server(c, monkeypatch, {"new": OTHER_PNG, "wrong": PNG, "notpng": b"GIF89a"})
    c.fetch_icons(data, log)
    kept = home.cache / "icons"
    assert sorted(p.name for p in kept.iterdir()) == ["new.png"]
    assert (kept / "new.png").read_bytes() == OTHER_PNG
    # Only what the package lacks is asked for, each limited
    assert sorted(u.rsplit("/", 1)[1] for u, _n, _t in server.reads) == ["new.png", "notpng.png", "wrong.png"]
    assert all(n == c.MAX_ICON + 1 for _u, n, _t in server.reads)
    assert log.has("icon of wrong not fetched (does not match the catalog)")
    assert log.has("icon of notpng not fetched (does not match the catalog)")
    assert log.has("icon of gone not fetched")
    # A second run finds everything it can show and asks for nothing more of it
    server.reads.clear()
    c.fetch_icons(c.parse(catalog(1, [app("packaged", icon=sha(PNG)), app("new", icon=sha(OTHER_PNG))])), log)
    assert server.reads == []


def test_fetch_icons_removes_kept_icons_that_are_no_longer_wanted(c, home, monkeypatch, log):
    kept = home.cache / "icons"
    kept.mkdir(parents=True)
    (kept / "old.png").write_bytes(PNG)
    (kept / "changed.png").write_bytes(PNG)
    (kept / "stays.png").write_bytes(PNG)
    icon_server(c, monkeypatch, {"changed": OTHER_PNG})
    c.fetch_icons(c.parse(catalog(1, [app("changed", icon=sha(OTHER_PNG)), app("stays", icon=sha(PNG))])), log)
    assert sorted(p.name for p in kept.iterdir()) == ["changed.png", "stays.png"]
    assert (kept / "changed.png").read_bytes() == OTHER_PNG


def test_fetch_icons_without_a_cache_folder(c, home, monkeypatch, log):
    home.cache.parent.mkdir(parents=True, exist_ok=True)
    home.cache.write_text("a file where the folder should be")
    icon_server(c, monkeypatch, {"new": PNG})
    c.fetch_icons(c.parse(catalog(1, [app("new", icon=sha(PNG))])), log)
    assert log.has("icon of new not fetched")


def test_load_fetches_icons_only_with_the_server(c, home, keyring, monkeypatch, log):
    data = catalog(2_000_000_000, [app("new", icon=sha(PNG))])
    sig = keyring.keys.sign(keyring.trusted, data)
    files = {c.URL: data, c.URL + ".sig": sig, c.ICON_URL.format(id="new"): PNG}
    monkeypatch.setattr(c.urllib.request, "urlopen", Server(files))
    c.load(log=log)
    assert (home.cache / "icons" / "new.png").read_bytes() == PNG
    (home.cache / "icons" / "new.png").unlink()

    def no_network(*_a, **_kw):
        raise AssertionError("network access offline")
    monkeypatch.setattr(c.urllib.request, "urlopen", no_network)
    best = c.load(fetch=False, log=log)
    assert best["apps"][0]["id"] == "new" and not (home.cache / "icons" / "new.png").exists()
