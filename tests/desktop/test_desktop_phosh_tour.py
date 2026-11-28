# phosh-tour-plinius: the tour's pages for what phosh-plinius adds. The pictures are what the tour
# shows (SVG of 240x400 like phosh-tour's own, nothing loaded from elsewhere), every page the
# patch adds has its picture and every picture a page, and every string the patch adds is
# translated in all languages of the package. With the source cache, prepare() runs on the pinned
# commit and the tour as it would be built is checked: its resources compile and every language
# is translated completely, Spanish (new) included.

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

from conftest import PKG
from prepare_helper import SRCDEST, Unavailable, run_prepare

TOUR = PKG / "phosh-tour-plinius"
PATCH = next(TOUR.glob("0001-*.patch"))
SVG = "{http://www.w3.org/2000/svg}"
XGETTEXT = ["xgettext", "--from-code=UTF-8", "-k_", "-kN_", "-kC_:1c,2", "-kNC_:1c,2"]


def pkgbuild_array(name):
    dump = f'source ./PKGBUILD >/dev/null; printf "%s\\n" "${{{name}[@]}}"'
    out = subprocess.run(["bash", "-c", dump], cwd=TOUR, capture_output=True, text=True, check=True,
                         env={"PATH": "/usr/bin:/bin", "CARCH": "aarch64"}).stdout
    return out.split()


PAGES = pkgbuild_array("_pages")
LANGS = pkgbuild_array("_langs")


def added(path):
    """The lines the patch adds to path."""
    text = PATCH.read_text()
    part = text.split(f"+++ b/{path}\n", 1)[1].split("\ndiff --git", 1)[0]
    return [line[1:] for line in part.splitlines() if line.startswith("+")]


def unescape(s):
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")


def new_strings():
    return [unescape(m) for line in added("src/ui/pt-window.ui")
            for m in re.findall(r'translatable="yes">([^<]+)<', line)]


def translations(po):
    """{msgid: msgstr} of a .po file, without the header."""
    r = subprocess.run(["msgcat", "--no-wrap", "--no-location", str(po)], capture_output=True,
                       text=True, check=True)
    out = {}
    for block in r.stdout.split("\n\n"):
        mid = re.search(r'^msgid "(.*)"$', block, re.M)
        mstr = re.search(r'^msgstr "(.*)"$', block, re.M)
        if mid and mid.group(1) and mstr:
            out[mid.group(1).replace('\\"', '"')] = mstr.group(1).replace('\\"', '"')
    return out


@pytest.mark.parametrize("page", PAGES)
def test_picture_like_the_tours_own(page):
    root = ET.parse(TOUR / page).getroot()
    assert root.tag == SVG + "svg"
    size = (root.get("width"), root.get("height"), root.get("viewBox"))
    assert size == ("240", "400", "0 0 240 400")
    # Nothing from outside the file: GtkPicture loads it from a resource, offline
    refs = [v for el in root.iter() for k, v in el.attrib.items() if k.endswith("href")]
    assert all(v.startswith("#") for v in refs), refs
    assert "url(http" not in (TOUR / page).read_text()


def blue_finger(pixels, width, height):
    """Whether the picture has the finger: the dot of phosh-tour's pictures, #1c71d8."""
    every_other = ((x, y) for x in range(0, width, 2) for y in range(0, height, 2))
    return any(abs(r - 0x1c) < 12 and abs(g - 0x71) < 12 and abs(b - 0xd8) < 12 and a > 200
               for r, g, b, a in (pixels[xy] for xy in every_other))


@pytest.mark.skipif(not shutil.which("rsvg-convert"), reason="rsvg-convert missing")
@pytest.mark.parametrize("page", PAGES)
def test_picture_draws_the_phone(page, tmp_path):
    Image = pytest.importorskip("PIL.Image")
    png = tmp_path / "page.png"
    subprocess.run(["rsvg-convert", "-w", "240", str(TOUR / page), "-o", str(png)], check=True)
    img = Image.open(png).convert("RGBA")
    assert img.size == (240, 400)
    px = img.load()
    # The light frame of the phone at its left edge, as in phosh-tour's pictures
    assert all(c > 0xc8 for c in px[17, 200][:3]) and px[17, 200][3] > 200, px[17, 200]
    # The screen is black near its edge below the top bar (nothing lights it there)
    assert px[22, 33][:3] == (0, 0, 0), px[22, 33]
    # Outside the phone the picture stays transparent: it lies on the tour's own background
    assert px[2, 2][3] == 0
    # Every page shows the finger, except the camera light (nobody touches anything), the
    # always-on display (the screen is off) and the power menu (the pressed key is blue
    # instead, as in phosh-tour's own picture)
    no_finger = ("camera-light.svg", "always-on.svg", "power-menu.svg")
    assert blue_finger(px, *img.size) == (page not in no_finger)


def test_every_page_has_its_picture_and_every_picture_a_page():
    ui = "\n".join(added("src/ui/pt-window.ui"))
    shown = re.findall(r"pages/plinius/([\w-]+\.svg)</property>", ui)
    gresource = "\n".join(added("src/phosh-tour.gresource.xml"))
    res = re.findall(r'alias="([\w-]+\.svg)">\.\./data/pages/plinius/\1<', gresource)
    assert sorted(shown) == sorted(PAGES) == sorted(res)
    assert all((TOUR / p).is_file() for p in PAGES)


def test_nine_new_pages_and_the_power_menu():
    ui = "\n".join(added("src/ui/pt-window.ui"))
    assert ui.count('<object class="PtPage">') == 9
    # The power menu page keeps its title and gets our picture and the actions of the list
    assert "pages/plinius/power-menu.svg" in ui
    assert "restart Phosh" in ui and "When set up" not in ui


@pytest.mark.parametrize("lang", LANGS)
def test_every_new_string_is_translated(lang):
    po = TOUR / f"{lang}.po"
    subprocess.run(["msgfmt", "--check", "-o", "/dev/null", str(po)], check=True,
                   capture_output=True)
    have = translations(po)
    strings = new_strings()
    assert len(strings) == 19
    missing = [s for s in strings if not have.get(s)]
    assert not missing, f"{lang}: {missing}"
    # The paths in GNOME Settings keep their separators, so they read as in Settings
    for s in strings:
        if "›" in s:
            assert have[s].count("›") == s.count("›"), (lang, have[s])


def test_image_takes_the_tour_from_us(tmp_path):
    # The package selection of image/mkimage.sh, run as it is with a recommended.list of our own
    script = (PKG.parent / "image" / "mkimage.sh").read_text()
    start = script.index("packages=(base ")
    end = script.index('done < "$PKGBUILDS/plinius-apps/recommended.list"')
    part = (script[start:end] + 'done < "$PKGBUILDS/plinius-apps/recommended.list"\n'
            'printf "%s\\n" "${packages[@]}"\n')
    (tmp_path / "plinius-apps").mkdir()
    (tmp_path / "plinius-apps" / "recommended.list").write_text(
        "# First start\nphosh-tour\nsnapshot\ngnome-clocks\n# Not preinstalled\nfoo\n")
    out = subprocess.run(["bash", "-c", part], capture_output=True, text=True, check=True,
                         env={"PATH": "/usr/bin:/bin", "PKGBUILDS": str(tmp_path)}).stdout.split()
    assert "phosh-tour-plinius" in out and "phosh-tour" not in out
    assert "gnome-clocks" in out and "foo" not in out


def test_languages_are_the_projects_ones():
    assert {"de", "es", "fr", "it", "nl", "pl", "pt", "ru"} <= set(LANGS)


@pytest.mark.slow
@pytest.mark.skipif(not SRCDEST.is_dir(), reason="no source cache (build/cache/sources)")
@pytest.mark.skipif(not shutil.which("glib-compile-resources"),
                    reason="glib-compile-resources missing")
def test_prepared_tour_is_complete(tmp_path):
    try:
        r = run_prepare("phosh-tour-plinius", tmp_path)
    except Unavailable as e:
        pytest.skip(str(e))
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    src = tmp_path / "src" / "phosh-tour"
    # Every picture the resources name is there
    subprocess.run(["glib-compile-resources", "--sourcedir", "src", "--target",
                    str(tmp_path / "tour.gresource"), "src/phosh-tour.gresource.xml"],
                   cwd=src, check=True, capture_output=True)
    assert "es" in (src / "po" / "LINGUAS").read_text().split()
    pot = tmp_path / "tour.pot"
    subprocess.run([*XGETTEXT, "-o", str(pot), "--files-from=po/POTFILES.in"], cwd=src,
                   check=True, capture_output=True)
    for lang in LANGS:
        merged = tmp_path / f"{lang}.po"
        subprocess.run(["msgmerge", "-q", "--no-fuzzy-matching", str(src / "po" / f"{lang}.po"),
                        str(pot), "-o", str(merged)], check=True, capture_output=True)
        stats = subprocess.run(["msgfmt", "--check", "--statistics", "-o", "/dev/null",
                                str(merged)], capture_output=True, text=True, check=True,
                               env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}).stderr
        assert "untranslated" not in stats and "fuzzy" not in stats, f"{lang}: {stats}"
    # phosh-tour's own header stays (its translators and where to report), ours is not taken
    head = (src / "po" / "de.po").read_text().split("\n\n", 1)[0]
    assert "gitlab.gnome.org/World/Phosh/phosh-tour" in head and "Plinius" not in head
