# What the password prompt takes from the session: background-sync (in the user's session) hands
# the wallpaper and the keyboard's look to background-install and look-install (root), and
# look-files puts them into the initramfs. The values end up in an unencrypted ramdisk, so each
# helper accepts only exact formats; the tests also check that what one side writes, the other
# reads. Root helpers run as copies with their paths in tmp_path; systemctl is a stub.

import ast
import struct
import subprocess

import pytest

from conftest import PKG

CRYPT = PKG / "plinius-crypt"
# PyGObject of the host warns about its own GLib bindings
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
LV_HEADER = bytes.fromhex("191100006c00f000b0010000")
SIZE = 12 + 108 * 240 * 4


def helper_copy(t, name):
    text = (CRYPT / name).read_text()
    text = text.replace("dir=/var/lib/plinius-crypt", f"dir={t}/var/lib/plinius-crypt")
    text = text.replace("-d /run/plinius-crypt", f"-d {t}/run/plinius-crypt")
    code = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
    assert not [l for l in code if "=/var/" in l or " /run/" in l or " /var/" in l], code
    f = t / name
    f.write_text(text)
    return f


@pytest.fixture
def helpers(tmp_path, fake_bin):
    fake_bin.stub("systemctl")

    def run(name, *args, stdin=b""):
        return subprocess.run(["bash", str(helper_copy(tmp_path, name)), *args], input=stdin,
                              env=fake_bin.env(isolated=True), capture_output=True)
    run.state = tmp_path / "var/lib/plinius-crypt"
    run.t = tmp_path
    run.fb = fake_bin
    return run


# --- look-install --------------------------------------------------------------------------------

def test_look_install_stores_exact_values(helpers):
    r = helpers("look-install", "glass", "#A1b2C3", "de")
    assert r.returncode == 0, r.stderr
    assert (helpers.state / "look").read_text() == "style=glass\naccent=#A1b2C3\nlayout=de\n"
    # The initramfs is rebuilt only where the hook runs
    assert helpers.fb.calls("systemctl") == []
    (helpers.t / "run/plinius-crypt").mkdir(parents=True)
    helpers("look-install", "original", "#000000", "neo")
    assert (helpers.state / "look").read_text() == "style=original\naccent=#000000\nlayout=\n"
    assert helpers.fb.calls("systemctl") == [["start", "--no-block",
                                              "plinius-crypt-initramfs.service"]]
    # Unchanged: no rebuild
    helpers("look-install", "original", "#000000", "neo")
    assert len(helpers.fb.calls("systemctl")) == 1


@pytest.mark.parametrize("args", [
    ["fancy", "#123456", "us"], ["glass", "123456", "us"], ["glass", "#12345", "us"],
    ["glass", "#12345g", "us"], ["glass", "#123456\naccent=#000000", "us"],
    ["glass", "#123456"], ["glass", "#123456", "us", "extra"],
])
def test_look_install_refuses(helpers, args):
    r = helpers("look-install", *args)
    assert r.returncode == 1
    assert not (helpers.state / "look").exists()


# --- background-install --------------------------------------------------------------------------

def test_background_install_takes_only_the_exact_format(helpers):
    image = LV_HEADER + bytes(range(256)) * (108 * 240 * 4 // 256)
    assert len(image) == SIZE
    r = helpers("background-install", stdin=image)
    assert r.returncode == 0, r.stderr
    assert (helpers.state / "glass-background.bin").read_bytes() == image
    assert (helpers.state / "glass-background.bin").stat().st_mode & 0o777 == 0o644
    for bad in (image + b"\0", image[:-1], b"\x19\x10" + image[2:],
                LV_HEADER.replace(b"\x6c\x00", b"\x6d\x00", 1) + image[12:]):
        r = helpers("background-install", stdin=bad)
        assert r.returncode == 1
        assert (helpers.state / "glass-background.bin").read_bytes() == image
    # No temporary file left behind
    assert sorted(p.name for p in helpers.state.iterdir()) == ["glass-background.bin"]


# --- look-files ----------------------------------------------------------------------------------

def look_files(sysroot, dest):
    r = subprocess.run(["bash", str(CRYPT / "look-files"), str(dest)],
                       env={"PLINIUS_CRYPT_SYSROOT": str(sysroot), "PATH": "/usr/bin:/bin",
                            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return {str(p.relative_to(dest)): p.read_text() for p in dest.rglob("*") if p.is_file()}


def put(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_look_files_nothing_to_say(tmp_path):
    assert look_files(tmp_path / "sys", tmp_path / "out") == {}


def test_look_files_takes_what_look_install_wrote(helpers, tmp_path):
    helpers("look-install", "glass", "#a1b2c3", "fr")
    sysroot = helpers.t  # look-install wrote below it
    files = look_files(sysroot, tmp_path / "out")
    assert files["etc/unl0kr.conf.d/60-plinius-look.conf"] == \
        "[theme]\ndefault=plinius-glass\nalternate=plinius-original\naccent=#a1b2c3\n"
    assert files["etc/unl0kr.conf.d/40-plinius-keyboard.conf"] == \
        "[keyboard]\nlayout=fr\n\n[hardware keyboard]\nlayout=fr\n"


def test_look_files_checks_the_values_again(tmp_path):
    sysroot = tmp_path / "sys"
    put(sysroot, "var/lib/plinius-crypt/look",
        "style=evil\naccent=#zzzzzz\nlayout=xx\naccent=#12345\nother=1\n")
    files = look_files(sysroot, tmp_path / "out")
    assert files == {}


@pytest.mark.parametrize("vconsole,xorg,expect", [
    ("KEYMAP=de-latin1\n", None, ("de", "layout=de")),
    ('KEYMAP="es"\n', None, ("es", "layout=es")),
    ("KEYMAP=ru\n", None, ("ru", "layout=us,ru\noptions=grp:alt_shift_toggle")),
    ("KEYMAP=us\n", None, None),
    ("KEYMAP=jp106\n", None, None),
    ("FONT=x\n", 'Section "InputClass"\n  Option "XkbLayout" "fr,us"\nEndSection\n',
     ("fr", "layout=fr")),
])
def test_look_files_keyboard_layout(tmp_path, vconsole, xorg, expect):
    sysroot = tmp_path / "sys"
    put(sysroot, "etc/vconsole.conf", vconsole)
    if xorg:
        put(sysroot, "etc/X11/xorg.conf.d/00-keyboard.conf", xorg)
    files = look_files(sysroot, tmp_path / "out")
    conf = files.get("etc/unl0kr.conf.d/40-plinius-keyboard.conf")
    if expect is None:
        assert conf is None
    else:
        assert conf == f"[keyboard]\nlayout={expect[0]}\n\n[hardware keyboard]\n{expect[1]}\n"


def test_look_files_session_layout_wins_over_the_keymap(tmp_path):
    sysroot = tmp_path / "sys"
    put(sysroot, "etc/vconsole.conf", "KEYMAP=de\n")
    put(sysroot, "var/lib/plinius-crypt/look", "style=original\naccent=#010203\nlayout=us\n")
    files = look_files(sysroot, tmp_path / "out")
    assert "etc/unl0kr.conf.d/40-plinius-keyboard.conf" not in files  # us is unl0kr's default


def test_look_files_language_and_wallpaper(tmp_path):
    sysroot = tmp_path / "sys"
    put(sysroot, "etc/locale.conf", "LANG=de_DE.UTF-8\n")
    put(sysroot, "usr/share/plinius-crypt/l10n/de", "Yes\tJa\n")
    put(sysroot, "var/lib/plinius-crypt/glass-background.bin", "WALLPAPER")
    files = look_files(sysroot, tmp_path / "out")
    assert files["etc/plinius-crypt.l10n"] == "Yes\tJa\n"
    assert files["usr/share/plinius-crypt/glass-background.bin"] == "WALLPAPER"
    # A language without a table: English, nothing written
    put(sysroot, "etc/locale.conf", "LANG=pt_BR.UTF-8\n")
    assert "etc/plinius-crypt.l10n" not in look_files(sysroot, tmp_path / "out2")


def test_look_files_ignores_an_inherited_lang(tmp_path):
    sysroot = tmp_path / "sys"
    put(sysroot, "usr/share/plinius-crypt/l10n/de", "Yes\tJa\n")
    r = subprocess.run(["bash", str(CRYPT / "look-files"), str(tmp_path / "out")],
                       env={"PLINIUS_CRYPT_SYSROOT": str(sysroot), "PATH": "/usr/bin:/bin",
                            "LANG": "de_DE.UTF-8"}, capture_output=True, text=True)
    assert r.returncode == 0
    assert not (tmp_path / "out/etc/plinius-crypt.l10n").exists()


# --- background-sync (logic only, no session) ----------------------------------------------------

@pytest.fixture(scope="module")
def sync():
    """background-sync's constants and functions; the settings objects and the main loop of its
    top level are left out (they need a session)."""
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf  # noqa: F401
    except (ImportError, ValueError):
        pytest.skip("no PyGObject with GdkPixbuf on the host")
    src = (CRYPT / "background-sync").read_text()
    tree = ast.parse(src)
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
            keep.append(node)
        elif isinstance(node, ast.Assign):
            # Not the settings objects (Gio.Settings.new, settings_if_installed)
            call = ast.unparse(node.value)
            if "Settings" not in call and "settings_if_installed" not in call:
                keep.append(node)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) \
                and getattr(node.value.func, "attr", "") == "require_version":
            keep.append(node)
    ns = {"__name__": "background_sync_under_test"}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "background-sync", "exec"), ns)
    for name in ("background", "interface", "input_sources", "keyboard", "appearance", "ambient"):
        assert name not in ns
    return ns


class FakeSettings:
    def __init__(self, **values):
        self.values = values

    def get_string(self, key):
        return self.values[key]

    def get_boolean(self, key):
        return self.values[key]

    def get_value(self, key):
        class V:
            def __init__(self, v):
                self.v = v

            def unpack(self):
                return self.v
        return V(self.values[key])


def png(path, w, h, rgb):
    from gi.repository import GdkPixbuf
    pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, w, h)
    pb.fill((rgb << 8) | 0xFF)
    pb.savev(str(path), "png", [], [])
    return str(path)


def test_render_gives_what_background_install_takes(sync, helpers, tmp_path):
    data = sync["render"](png(tmp_path / "red.png", 300, 200, 0xFF0000))
    assert data[:12] == LV_HEADER and len(data) == SIZE
    # A private picture: darkened to 204/255 onto black; memory order B, G, R, X
    b, g, r, x = data[12:16]
    assert (b, g, x) == (0, 0, 255) and abs(r - 204) <= 1
    assert len(set(struct.iter_unpack("<I", data[12:]))) == 1
    r = helpers("background-install", stdin=data)
    assert r.returncode == 0, r.stderr
    assert (helpers.state / "glass-background.bin").read_bytes() == data


def test_render_of_a_broken_file(sync, tmp_path):
    (tmp_path / "x.png").write_bytes(b"not a picture")
    assert sync["render"](str(tmp_path / "x.png")) is None


def test_slideshow_first(sync, tmp_path):
    xml = tmp_path / "show.xml"
    xml.write_text("<background><static><duration>1</duration><file>/a.jpg</file></static>"
                   "<static><file>/b.jpg</file></static></background>")
    assert sync["slideshow_first"](str(xml)) == "/a.jpg"
    xml.write_text("<background><static><file><size width='1'>/small.jpg</size>"
                   "<size width='9'> /large.jpg </size></file></static></background>")
    assert sync["slideshow_first"](str(xml)) == "/large.jpg"
    xml.write_text("<background>")
    assert sync["slideshow_first"](str(xml)) is None
    assert sync["slideshow_first"](str(tmp_path / "missing.xml")) is None


def test_wallpaper_path(sync, tmp_path, monkeypatch):
    light, dark = tmp_path / "light.png", tmp_path / "dark.png"
    monkeypatch.setitem(sync, "ambient", None)
    monkeypatch.setitem(sync, "interface", FakeSettings(**{"color-scheme": "prefer-dark"}))
    monkeypatch.setitem(sync, "background", FakeSettings(**{"picture-uri": f"file://{light}",
                                                            "picture-uri-dark": f"file://{dark}",
                                                            "picture-options": "zoom"}))
    assert sync["wallpaper_path"]() == str(dark)
    monkeypatch.setitem(sync, "interface", FakeSettings(**{"color-scheme": "default"}))
    assert sync["wallpaper_path"]() == str(light)
    # A 360° photo shown by plinius-ambient wins, if it exists
    pano = tmp_path / "pano.jpg"
    monkeypatch.setitem(sync, "ambient", FakeSettings(**{"panorama-enabled": True,
                                                         "panorama": str(pano)}))
    assert sync["wallpaper_path"]() == str(light)
    pano.write_bytes(b"x")
    assert sync["wallpaper_path"]() == str(pano)


@pytest.mark.parametrize("text,rgb", [
    ("#1793d1", (0x17, 0x93, 0xd1)), ("#ABCDEF", (0xab, 0xcd, 0xef)), (" #fff ", (255, 255, 255)),
    ("#f0a", (0xff, 0x00, 0xaa)),
    # Anything else is black, as Phosh shows it
    ("", (0, 0, 0)), ("red", (0, 0, 0)), ("#12345", (0, 0, 0)), ("#1234567", (0, 0, 0)),
    ("#ggghhh", (0, 0, 0)), ("rgb(1,2,3)", (0, 0, 0)),
])
def test_parse_color(sync, text, rgb):
    assert sync["parse_color"](text) == rgb


def no_picture(sync, monkeypatch, options="none", color="#1793d1", ambient=None):
    monkeypatch.setitem(sync, "ambient", ambient)
    monkeypatch.setitem(sync, "interface", FakeSettings(**{"color-scheme": "default"}))
    monkeypatch.setitem(sync, "background", FakeSettings(**{
        "picture-uri": "file:///usr/share/backgrounds/plinius/arch-black.png",
        "picture-uri-dark": "", "picture-options": options, "primary-color": color}))


@pytest.mark.parametrize("options", ["zoom", "wallpaper", "centered", "scaled", "stretched",
                                     "spanned"])
def test_plain_color_only_with_no_picture(sync, monkeypatch, options):
    no_picture(sync, monkeypatch, options)
    assert sync["plain_color"]() is None
    assert sync["wallpaper_path"]() == "/usr/share/backgrounds/plinius/arch-black.png"


def test_no_picture_gives_the_primary_colour(sync, monkeypatch, tmp_path):
    no_picture(sync, monkeypatch)
    assert sync["plain_color"]() == (0x17, 0x93, 0xd1)
    assert sync["wallpaper_path"]() is None
    # A 360° photo of plinius-ambient still wins over "No picture"
    pano = tmp_path / "pano.jpg"
    pano.write_bytes(b"x")
    no_picture(sync, monkeypatch, ambient=FakeSettings(**{"panorama-enabled": True,
                                                          "panorama": str(pano)}))
    assert sync["wallpaper_path"]() == str(pano)


def test_render_color_is_darkened_like_a_system_wallpaper(sync, helpers):
    data = sync["render_color"]((0x17, 0x93, 0xd1))
    assert data[:12] == LV_HEADER and len(data) == SIZE
    # 217/255 onto black, memory order B, G, R, X, the same in every pixel
    b, g, r, x = data[12:16]
    want = [round(c * 217 / 255) for c in (0xd1, 0x93, 0x17)]
    assert all(abs(have - w) <= 1 for have, w in zip((b, g, r), want)) and x == 255
    assert len(set(struct.iter_unpack("<I", data[12:]))) == 1
    r = helpers("background-install", stdin=data)
    assert r.returncode == 0, r.stderr


class FakeSubprocess:
    """Gio.Subprocess for sync_background: records what reaches pkexec, no process starts."""
    runs = []
    ok = True

    @classmethod
    def new(cls, argv, flags):
        cls.runs.append([argv, None])
        return cls()

    def communicate(self, data, cancellable):
        self.runs[-1][1] = bytes(data.get_data())

    def get_successful(self):
        return self.ok


@pytest.fixture
def pkexec(sync, monkeypatch):
    class Gio:
        Subprocess = FakeSubprocess
        SubprocessFlags = sync["Gio"].SubprocessFlags
    FakeSubprocess.runs, FakeSubprocess.ok = [], True
    monkeypatch.setitem(sync, "Gio", Gio)
    logged = []
    monkeypatch.setitem(sync, "log", logged.append)
    return FakeSubprocess, logged


def test_sync_background_hands_the_colour_to_the_helper(sync, monkeypatch, pkexec):
    runs, logged = pkexec
    no_picture(sync, monkeypatch, color="#000")
    sync["sync_background"]()
    assert [argv for argv, _ in runs.runs] == [["pkexec", sync["HELPER"]]]
    assert runs.runs[0][1] == sync["render_color"]((0, 0, 0))
    assert logged == []


def test_sync_background_with_a_picture_and_a_failing_helper(sync, monkeypatch, pkexec, tmp_path):
    runs, logged = pkexec
    picture = png(tmp_path / "own.png", 40, 80, 0x00FF00)
    no_picture(sync, monkeypatch, options="zoom")
    monkeypatch.setitem(sync, "wallpaper_path", lambda: picture)
    runs.ok = False
    sync["sync_background"]()
    assert runs.runs[0][1] == sync["render"](picture)
    assert logged == [f"{sync['HELPER']} failed for {picture}"]


def test_sync_background_without_anything_to_show(sync, monkeypatch, pkexec, tmp_path):
    # A picture that cannot be loaded: nothing is handed over, the old one stays
    runs, _ = pkexec
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"x")
    no_picture(sync, monkeypatch, options="zoom")
    monkeypatch.setitem(sync, "wallpaper_path", lambda: str(broken))
    sync["sync_background"]()
    assert runs.runs == []


def test_accent_color_and_layout(sync, monkeypatch):
    monkeypatch.setitem(sync, "appearance", None)
    monkeypatch.setitem(sync, "interface", FakeSettings(**{"accent-color": "teal"}))
    assert sync["accent_color"]() == "#2190a4"
    monkeypatch.setitem(sync, "interface", FakeSettings(**{"accent-color": "mauve"}))
    assert sync["accent_color"]() == "#3584e4"
    monkeypatch.setitem(sync, "appearance", FakeSettings(**{"accent-color": "#ABCDEF"}))
    assert sync["accent_color"]() == "#ABCDEF"
    monkeypatch.setitem(sync, "appearance", FakeSettings(**{"accent-color": "#ABCDEF;x"}))
    assert sync["accent_color"]() == "#3584e4"
    monkeypatch.setitem(sync, "input_sources",
                        FakeSettings(sources=[("ibus", "anthy"), ("xkb", "de+neo"), ("xkb", "us")]))
    assert sync["first_layout"]() == "de"
    monkeypatch.setitem(sync, "input_sources", FakeSettings(sources=[]))
    assert sync["first_layout"]() == ""
    monkeypatch.setitem(sync, "keyboard", None)
    assert sync["keyboard_look"]() == "original"


def test_accent_colors_pass_look_install(sync, helpers):
    for color in sync["ACCENT_COLORS"].values():
        assert helpers("look-install", "glass", color, "us").returncode == 0


# --- glass-background.py (the package's default background) -------------------------------------

def test_glass_background(tmp_path):
    import shutil
    if shutil.which("rsvg-convert") is None:
        pytest.skip("no rsvg-convert on the host")
    pytest.importorskip("PIL")
    svg = tmp_path / "w.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
                   '<rect width="50" height="100" fill="#ff0000"/>'
                   '<rect x="50" width="50" height="100" fill="#0000ff"/></svg>')
    out = tmp_path / "bg.bin"
    r = subprocess.run(["python3", "-I", str(CRYPT / "glass-background.py"), str(svg), str(out),
                        str(tmp_path / "preview.png")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = out.read_bytes()
    assert struct.unpack_from("<BBHHHHH", data) == (0x19, 0x11, 0, 216, 480, 864, 0)
    assert len(data) == 12 + 216 * 480 * 4
    # Memory order B, G, R, X: red on the left, blue on the right, darkened a little
    b, g, r_, _ = data[12:16]
    assert r_ > 150 and b < 60
    b, g, r_, _ = data[12 + 215 * 4:12 + 216 * 4]
    assert b > 150 and r_ < 60
    assert (tmp_path / "preview.png").exists()


def test_glass_background_without_a_preview(tmp_path):
    import shutil
    if shutil.which("rsvg-convert") is None:
        pytest.skip("no rsvg-convert on the host")
    pytest.importorskip("PIL")
    svg = tmp_path / "w.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                   '<rect width="10" height="10" fill="#00ff00"/></svg>')
    out = tmp_path / "bg.bin"
    r = subprocess.run(["python3", "-I", str(CRYPT / "glass-background.py"), str(svg), str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert len(out.read_bytes()) == 12 + 216 * 480 * 4
    # Only the image: no preview written anywhere
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bg.bin", "w.svg"]
