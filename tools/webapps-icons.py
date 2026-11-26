#!/usr/bin/env python3
"""Fetches the icons of the catalog of web apps (pkgbuilds/plinius-webapps/catalog.json) and packs
them into pkgbuilds/plinius-webapps/webapps-icons.tar: icons/<id>.png, 128 x 128 pixels, square
on a transparent ground. Settings shows them in front of the names in Apps > Web Apps > Add; the
package brings the archive, tools/publish-webapps.sh puts the icons next to the published catalog
with their SHA-256 in it, so a phone takes a new icon only when it matches.

An icon comes from the website itself (apple-touch-icon, the web manifest's icons, <link
rel="icon"> with sizes, /apple-touch-icon.png) or, when the site gives none of at least
MIN_SIDE pixels or turns the tool away, from Google's favicon service for the host
(only the host name is sent there). Run on the build PC, never on the phone.

Usage: tools/webapps-icons.py [--only ID ...] [--file ID PATH] [--list-small]
  --only ID    fetch just these entries again, keep the other icons of the archive
  --file ID P  take the image file P as the icon of ID (for a site whose icon cannot be fetched)
  --list-small name the icons whose source was smaller than MIN_SIDE
Entries without an icon keep the website's icon search of Settings when added.
"""

import argparse
import html
import http.client
import io
import json
import re
import sys
import tarfile
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "pkgbuilds" / "plinius-webapps"
CATALOG = PKG / "catalog.json"
ARCHIVE = PKG / "webapps-icons.tar"
SIZE = 128
MIN_SIDE = 96
# A smaller source is not kept: enlarged to SIZE it would only be a blur (the row shows the
# generic icon instead, adding the web app still looks on the website)
MIN_KEEP = 32
MAX_BYTES = 2 << 20
TIMEOUT = 15
# Sites answer browsers with their real page
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
# Google's favicon service; it sends on to one of its hosts t0 to t3, not all of which always
# resolve, so a failure is tried again. For a site it has no icon of it answers with a stand-in
# picture, the same as for a host that does not exist (NO_SUCH_HOST): that one is not taken.
FAVICON_SERVICE = "https://www.google.com/s2/favicons?domain={host}&sz=256"
FAVICON_TRIES = 4
NO_SUCH_HOST = "no-such-host.invalid"
_stand_in = []

LINK = re.compile(r"<link\b[^>]*>", re.I)
ATTR = re.compile(r"""([a-zA-Z:-]+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")


def get(url, limit=MAX_BYTES):
    """(body, final address); every failure of the transfer is an OSError."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read(limit + 1)
            final = r.geturl()
    except (http.client.HTTPException, ValueError) as e:
        raise OSError(str(e) or type(e).__name__) from None
    if len(data) > limit:
        raise OSError("too large")
    return data, final


def attributes(tag):
    return {k.lower(): html.unescape(v.strip("\"'")) for k, v in ATTR.findall(tag)}


def size_of(sizes):
    best = 0
    for part in (sizes or "").lower().split():
        if part == "any":
            return 1024
        m = re.match(r"(\d+)x(\d+)$", part)
        if m:
            best = max(best, int(m.group(1)))
    return best


def candidates(page, base):
    """[(claimed size, url)] of the icons a page names, largest first; guesses at the end."""
    out, manifest = [], None
    text = page.decode("utf-8", "replace")
    for tag in LINK.findall(text):
        a = attributes(tag)
        rel = a.get("rel", "").lower().split()
        href = a.get("href")
        if not href:
            continue
        url = urllib.parse.urljoin(base, href)
        if "manifest" in rel:
            manifest = url
        elif "apple-touch-icon" in rel or "apple-touch-icon-precomposed" in rel:
            out.append((size_of(a.get("sizes")) or 180, url))
        elif "icon" in rel:
            out.append((size_of(a.get("sizes")) or 32, url))
    if manifest:
        try:
            data, final = get(manifest, 256 << 10)
            for icon in json.loads(data).get("icons", []):
                if isinstance(icon, dict) and icon.get("src"):
                    out.append((size_of(icon.get("sizes")), urllib.parse.urljoin(final, icon["src"])))
        except (OSError, ValueError, AttributeError):
            pass
    out.sort(key=lambda c: -c[0])
    out.append((180, urllib.parse.urljoin(base, "/apple-touch-icon.png")))
    return out


def load_image(data):
    """A raster image of data (PNG, JPEG, ICO, WebP, GIF; the largest frame of an ICO), or None."""
    try:
        img = Image.open(io.BytesIO(data))
        if img.format == "ICO":
            img.size = max(img.ico.sizes())
        img.load()
        return img.convert("RGBA")
    except Exception:  # noqa: BLE001  any image the decoder refuses
        return None


def square(img):
    """img on a transparent square, scaled to SIZE (a small one is enlarged)."""
    box = img.getbbox()
    if box:
        img = img.crop(box)
    side = max(img.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return canvas.resize((SIZE, SIZE), Image.LANCZOS)


def png_bytes(img):
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def favicon_service(host, log=None, name=None):
    """The service's answer for host, tried FAVICON_TRIES times; None if it never came."""
    for n in range(FAVICON_TRIES):
        try:
            return get(FAVICON_SERVICE.format(host=urllib.parse.quote(host)))[0]
        except OSError as e:
            if log and n == FAVICON_TRIES - 1:
                log(f"{name}: favicon service failed ({e})")
    return None


def stand_in():
    """The service's stand-in pictures (fetched once): [bytes]."""
    if not _stand_in:
        data = favicon_service(NO_SUCH_HOST)
        _stand_in.append(data or b"")
    return _stand_in


def fetch_icon(app, log):
    """(PNG bytes, source side) of the best icon for app, or (None, 0)."""
    url = app["url"]
    host = urllib.parse.urlsplit(url).hostname
    best, side = None, 0
    try:
        page, final = get(url)
        for _claimed, icon_url in candidates(page, final)[:8]:
            try:
                img = load_image(get(icon_url)[0])
            except OSError:
                continue
            if img and min(img.size) > side:
                best, side = img, min(img.size)
            if side >= MIN_SIDE:
                break
    except OSError as e:
        log(f"{app['id']}: website not read ({e})")
    if side < MIN_SIDE and host:
        data = favicon_service(host, log, app["id"])
        img = load_image(data) if data and data not in stand_in() else None
        if img and min(img.size) > side:
            best, side = img, min(img.size)
    return (png_bytes(square(best)), side) if best else (None, 0)


def read_archive(path=None):
    """{id: PNG bytes} of the archive (ARCHIVE when no path is given)."""
    path = path or ARCHIVE
    icons = {}
    if path.exists():
        with tarfile.open(path) as tar:
            for m in tar.getmembers():
                name = Path(m.name)
                if m.isfile() and name.parent == Path("icons") and name.suffix == ".png":
                    icons[name.stem] = tar.extractfile(m).read()
    return icons


def write_archive(icons, path=None):
    """The same icons always give the same archive: sorted, no times, no owners. ARCHIVE when no
    path is given (looked up at the call, so a test's ARCHIVE counts)."""
    path = path or ARCHIVE
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for app_id in sorted(icons):
            info = tarfile.TarInfo(f"icons/{app_id}.png")
            info.size = len(icons[app_id])
            info.mode = 0o644
            info.mtime = 0
            info.uname = info.gname = "root"
            tar.addfile(info, io.BytesIO(icons[app_id]))
    path.write_bytes(buf.getvalue())


def main(argv=None, log=print):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", nargs="+", metavar="ID")
    parser.add_argument("--file", nargs=2, metavar=("ID", "PATH"))
    parser.add_argument("--list-small", action="store_true")
    args = parser.parse_args(argv)
    apps = json.loads(CATALOG.read_text(encoding="utf-8"))["apps"]
    ids = {a["id"] for a in apps}
    icons = {k: v for k, v in read_archive().items() if k in ids}

    if args.file:
        app_id, path = args.file
        if app_id not in ids:
            log(f"{app_id}: not in the catalog")
            return 1
        img = load_image(Path(path).read_bytes())
        if not img:
            log(f"{path}: not an image")
            return 1
        icons[app_id] = png_bytes(square(img))
        write_archive(icons)
        log(f"{app_id}: icon from {path}")
        return 0

    todo = [a for a in apps if not args.only or a["id"] in args.only]
    small = []
    with ThreadPoolExecutor(8) as pool:
        for app, (data, side) in zip(todo, pool.map(lambda a: fetch_icon(a, log), todo)):
            if data and side < MIN_KEEP:
                log(f"{app['id']}: icon of only {side} px, not kept")
                icons.pop(app["id"], None)
            elif data:
                icons[app["id"]] = data
                if side < MIN_SIDE:
                    small.append(f"{app['id']} ({side} px)")
            else:
                log(f"{app['id']}: no icon found")
    write_archive(icons)
    log(f"{len(icons)} of {len(apps)} entries have an icon, {ARCHIVE.stat().st_size // 1024} KiB")
    missing = sorted(ids - set(icons))
    if missing:
        log("without an icon: " + ", ".join(missing))
    if args.list_small and small:
        log("from small sources: " + ", ".join(small))
    return 0


if __name__ == "__main__":
    sys.exit(main())
