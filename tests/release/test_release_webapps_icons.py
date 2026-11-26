# tools/webapps-icons.py: the icons of the catalog of web apps. Which icons a page names (links,
# web manifest, the guess /apple-touch-icon.png), how an image becomes the square 128-pixel PNG,
# the choice between the website and the favicon service, the archive that always comes out the
# same, and the command line. The network is a fake; CATALOG and ARCHIVE point into tmp_path.

import io
import json
import tarfile

import pytest
from PIL import Image

from conftest import TOOLS, load_script

SCRIPT = TOOLS / "webapps-icons.py"


@pytest.fixture
def w(tmp_path, monkeypatch):
    m = load_script(SCRIPT)
    real = m.ARCHIVE
    before = real.read_bytes() if real.exists() else None
    monkeypatch.setattr(m, "CATALOG", tmp_path / "catalog.json")
    monkeypatch.setattr(m, "ARCHIVE", tmp_path / "webapps-icons.tar")

    def no_network(*_a, **_kw):
        raise AssertionError("network access in a test")
    monkeypatch.setattr(m.urllib.request, "urlopen", no_network)
    yield m
    # The package's archive is never touched
    assert (real.read_bytes() if real.exists() else None) == before


def image(size, colour=(255, 0, 0, 255), fmt="PNG", box=None):
    img = Image.new("RGBA", size, (0, 0, 0, 0) if box else colour)
    if box:
        img.paste(Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), colour), box[:2])
    out = io.BytesIO()
    if fmt == "ICO":
        img.save(out, "ICO", sizes=[(16, 16), size])
    else:
        img.save(out, fmt)
    return out.getvalue()


class Web:
    """get() of the tool: {url: bytes or an exception}."""

    def __init__(self, files):
        self.files = files
        self.asked = []

    def __call__(self, url, limit=None):
        self.asked.append(url)
        got = self.files.get(url)
        if isinstance(got, list):  # successive answers
            got = got.pop(0) if len(got) > 1 else got[0]
        if got is None:
            raise OSError("404")
        if isinstance(got, Exception):
            raise got
        return got, url


def test_attributes_and_sizes(w):
    assert w.attributes('<link rel="icon" href="/a&amp;b.png" sizes=\'32x32 64x64\'>') == {
        "rel": "icon", "href": "/a&b.png", "sizes": "32x32 64x64"}
    assert w.size_of("32x32 192x192") == 192
    assert w.size_of("any") == 1024
    assert w.size_of(None) == 0 and w.size_of("big") == 0


def test_candidates_from_links_manifest_and_the_guess(w, monkeypatch):
    page = (b'<html><head><link rel="icon" href="/f.ico"><link rel="apple-touch-icon" href="/t.png">'
            b'<link rel="icon" sizes="96x96" href="i96.png"><link rel="manifest" href="/m.json">'
            b'<link rel="stylesheet" href="/s.css"><link rel="icon"></head></html>')
    manifest = json.dumps({"icons": [{"src": "/m512.png", "sizes": "512x512"}, "junk", {"sizes": "1x1"}]}).encode()
    monkeypatch.setattr(w, "get", Web({"https://s.example/m.json": manifest}))
    assert w.candidates(page, "https://s.example/dir/") == [
        (512, "https://s.example/m512.png"), (180, "https://s.example/t.png"),
        (96, "https://s.example/dir/i96.png"), (32, "https://s.example/f.ico"),
        (180, "https://s.example/apple-touch-icon.png")]
    # A broken manifest leaves the rest
    monkeypatch.setattr(w, "get", Web({"https://s.example/m.json": b"not json"}))
    assert len(w.candidates(page, "https://s.example/dir/")) == 4


def test_load_image(w):
    assert w.load_image(image((40, 20))).size == (40, 20)
    # The largest frame of an ICO
    assert w.load_image(image((64, 64), fmt="ICO")).size == (64, 64)
    assert w.load_image(b"not an image") is None


def test_square_crops_centres_and_scales(w):
    img = w.load_image(image((200, 100), box=(50, 25, 150, 75)))
    sq = w.square(img)
    assert sq.size == (128, 128)
    # The coloured part fills the width, centred with transparency above and below
    assert sq.getpixel((64, 64))[3] == 255 and sq.getpixel((64, 5))[3] == 0
    # A small one is enlarged; a fully transparent one stays as it is
    assert w.square(w.load_image(image((16, 16)))).size == (128, 128)
    assert w.square(Image.new("RGBA", (10, 10), (0, 0, 0, 0))).size == (128, 128)


def test_fetch_icon_prefers_a_large_icon_of_the_website(w, monkeypatch):
    page = b'<link rel="icon" sizes="16x16" href="/s.png"><link rel="apple-touch-icon" href="/big.png">'
    web = Web({"https://s.example/": page, "https://s.example/big.png": image((180, 180), (0, 0, 255, 255)),
               "https://s.example/s.png": image((16, 16))})
    monkeypatch.setattr(w, "get", web)
    data, side = w.fetch_icon({"id": "s", "url": "https://s.example/"}, print)
    assert side == 180 and Image.open(io.BytesIO(data)).getpixel((64, 64))[:3] == (0, 0, 255)
    # Large enough at once: neither the small one nor the service is asked
    assert web.asked == ["https://s.example/", "https://s.example/big.png"]


def service(w, host):
    return w.FAVICON_SERVICE.format(host=host)


def test_fetch_icon_falls_back_to_the_favicon_service(w, monkeypatch):
    web = Web({"https://s.example/": b'<link rel="icon" href="/s.png">', "https://s.example/s.png": image((32, 32)),
               # A host that does not resolve at the first try, then the icon
               service(w, "s.example"): [OSError("no address"), image((256, 256), (0, 255, 0, 255))],
               service(w, w.NO_SUCH_HOST): image((16, 16), (9, 9, 9, 255))})
    monkeypatch.setattr(w, "get", web)
    data, side = w.fetch_icon({"id": "s", "url": "https://s.example/"}, print)
    assert side == 256 and Image.open(io.BytesIO(data)).getpixel((64, 64))[:3] == (0, 255, 0)
    assert web.asked.count(service(w, "s.example")) == 2


def test_the_service_s_stand_in_picture_is_not_taken(w, monkeypatch):
    stand_in = image((256, 256), (9, 9, 9, 255))
    web = Web({"https://s.example/": b"", service(w, "s.example"): stand_in, service(w, w.NO_SUCH_HOST): stand_in})
    monkeypatch.setattr(w, "get", web)
    assert w.fetch_icon({"id": "s", "url": "https://s.example/"}, print) == (None, 0)
    # The stand-in is asked for once
    w.fetch_icon({"id": "s", "url": "https://s.example/"}, print)
    assert web.asked.count(service(w, w.NO_SUCH_HOST)) == 1


def test_fetch_icon_keeps_the_better_one_and_logs_failures(w, monkeypatch):
    lines = []
    web = Web({"https://s.example/": OSError("403")})
    monkeypatch.setattr(w, "get", web)
    assert w.fetch_icon({"id": "s", "url": "https://s.example/"}, lines.append) == (None, 0)
    assert lines == ["s: website not read (403)", "s: favicon service failed (404)"]
    assert web.asked.count(service(w, "s.example")) == w.FAVICON_TRIES
    # The service's icon is smaller than the site's: the site's stays
    web.files = {"https://s.example/": b"", "https://s.example/apple-touch-icon.png": image((64, 64)),
                 service(w, "s.example"): image((16, 16))}
    assert w.fetch_icon({"id": "s", "url": "https://s.example/"}, lines.append)[1] == 64


def test_get_turns_every_failure_into_oserror(w, monkeypatch):
    import http.client

    class Answer(io.BytesIO):
        def geturl(self):
            return "https://s.example/final"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(w.urllib.request, "urlopen", lambda req, timeout: Answer(b"12345"))
    assert w.get("https://s.example/") == (b"12345", "https://s.example/final")
    with pytest.raises(OSError, match="too large"):
        w.get("https://s.example/", limit=3)

    def broken(req, timeout):
        raise http.client.IncompleteRead(b"")
    monkeypatch.setattr(w.urllib.request, "urlopen", broken)
    with pytest.raises(OSError):
        w.get("https://s.example/")


def test_archive_is_always_the_same(w, tmp_path):
    icons = {"b": b"B", "a": b"A"}
    w.write_archive(icons)
    first = w.ARCHIVE.read_bytes()
    w.write_archive(dict(reversed(list(icons.items()))))
    assert w.ARCHIVE.read_bytes() == first
    with tarfile.open(w.ARCHIVE) as tar:
        members = tar.getmembers()
    assert [m.name for m in members] == ["icons/a.png", "icons/b.png"]
    assert {(m.mtime, m.mode, m.uid, m.gid) for m in members} == {(0, 0o644, 0, 0)}
    assert w.read_archive() == icons
    # Other members are no icons
    with tarfile.open(w.ARCHIVE, "a") as tar:
        info = tarfile.TarInfo("readme.txt")
        tar.addfile(info, io.BytesIO(b""))
    assert w.read_archive() == icons
    assert w.read_archive(tmp_path / "missing.tar") == {}


def write_catalog(w, *ids):
    w.CATALOG.write_text(json.dumps({"schema": 1, "apps": [{"id": i, "url": f"https://{i}.example/"} for i in ids]}))


def test_main_fetches_keeps_and_drops(w, monkeypatch):
    write_catalog(w, "big", "small", "tiny", "none")
    w.write_archive({"tiny": b"old", "removed": b"gone"})
    results = {"big": (b"BIG", 200), "small": (b"SMALL", 48), "tiny": (b"TINY", 16), "none": (None, 0)}
    monkeypatch.setattr(w, "fetch_icon", lambda app, log: results[app["id"]])
    lines = []
    assert w.main(["--list-small"], log=lines.append) == 0
    # Entries gone from the catalog leave the archive; a blurry source is not kept
    assert w.read_archive() == {"big": b"BIG", "small": b"SMALL"}
    assert "tiny: icon of only 16 px, not kept" in lines
    assert "none: no icon found" in lines
    assert "without an icon: none, tiny" in lines
    assert "from small sources: small (48 px)" in lines
    # --only fetches just these again
    results["none"] = (b"NONE", 128)
    assert w.main(["--only", "none"], log=lines.append) == 0
    assert w.read_archive() == {"big": b"BIG", "small": b"SMALL", "none": b"NONE"}


def test_main_takes_an_icon_from_a_file(w, tmp_path):
    write_catalog(w, "shop")
    picture = tmp_path / "logo.png"
    picture.write_bytes(image((300, 300)))
    lines = []
    assert w.main(["--file", "shop", str(picture)], log=lines.append) == 0
    assert Image.open(io.BytesIO(w.read_archive()["shop"])).size == (128, 128)
    assert w.main(["--file", "other", str(picture)], log=lines.append) == 1
    assert lines[-1] == "other: not in the catalog"
    picture.write_bytes(b"text")
    assert w.main(["--file", "shop", str(picture)], log=lines.append) == 1
    assert lines[-1].endswith("not an image")
