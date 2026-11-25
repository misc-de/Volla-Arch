# plinius-send-to-tv: the Firefox add-on itself. background.js runs in node against a stand-in
# for Firefox's API (send_to_tv_harness.js): menus, page action, native messaging, notifications.
# make-xpi.py builds the add-on from the sources and checks a signed copy; the files that name
# each other (IDs, paths, keys of the strings) must agree.

import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from conftest import PKG, load_script

PKGDIR = PKG / "plinius-send-to-tv"
BACKGROUND = PKGDIR / "background.js"
HARNESS = Path(__file__).with_name("send_to_tv_harness.js")
LANGS = ("de", "es", "fr", "it", "nl", "pl", "pt", "ru")
VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
PLAYING = {"result": "playing", "name": "Living Room"}


def bg(scenario):
    if shutil.which("node") is None:
        pytest.skip("no node")
    r = subprocess.run(["node", str(HARNESS), str(BACKGROUND)], input=scenario, text=True,
                       capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.fixture(scope="module")
def mkxpi():
    return load_script(PKGDIR / "make-xpi.py")


# ---- which links count as a video ----

@pytest.mark.parametrize("url,video", [
    (VIDEO, True),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=42", True),
    ("http://youtube.com/watch?v=x", True),
    ("https://music.youtube.com/watch?v=x&list=y", True),
    ("https://youtu.be/dQw4w9WgXcQ", True),
    ("https://www.youtube.com/shorts/abc", True),
    ("https://www.youtube.com/live/abc", True),
    ("https://www.youtube.com/embed/abc", True),
    ("https://WWW.YOUTUBE.COM/watch?v=x", True),
    ("https://www.youtube.com/watch", False),           # no video id
    ("https://www.youtube.com/watch?v=", False),
    ("https://youtu.be/", False),
    ("https://www.youtube.com/feed/subscriptions", False),
    ("https://www.youtube.com/shorts/", False),
    ("https://evil.example/watch?v=x", False),
    ("https://youtube.com.evil.example/watch?v=x", False),
    ("ftp://www.youtube.com/watch?v=x", False),
    ("javascript:alert(1)", False),
    ("not a url", False),
])
def test_which_links_are_videos(url, video):
    assert bg(f"return isVideoUrl({json.dumps(url)});") is video


# ---- menus ----

@pytest.mark.parametrize("event", ["installed", "startup"])
def test_menus_are_made_on_install_and_start(event):
    fake = bg(f"await fake.listeners.{event}(); await fake.listeners.{event}();")
    assert fake["removed"] == 2
    link, page = fake["menus"]
    assert link["id"] == "send-to-tv-link" and link["contexts"] == ["link"]
    assert page["id"] == "send-to-tv-page" and page["contexts"] == ["page", "video"]
    assert "*://youtu.be/*" in link["targetUrlPatterns"]
    assert page["documentUrlPatterns"] == link["targetUrlPatterns"]
    assert link["title"] == page["title"] == "playOnTv"


@pytest.mark.parametrize("reply,state", [
    ({"connected": True, "name": "Living Room", "links": True},
     {"title": "playOn(Living Room)", "enabled": True}),
    ({"connected": True, "name": "", "links": True}, {"title": "playOn(theTv)", "enabled": True}),
    ({"connected": True, "name": "Living Room", "links": False},
     {"title": "noLinks(Living Room)", "enabled": False}),
    ({"connected": False, "name": "", "links": False}, {"title": "notConnected", "enabled": False}),
    (None, {"title": "notConnected", "enabled": False}),      # host not installed
])
def test_the_menu_says_where_it_plays(reply, state):
    replies = json.dumps([reply] if reply is not None else [])
    fake = bg(f"""fake.replies = {replies};
        await fake.listeners.shown({{menuIds: ["send-to-tv-link", "other"]}});""")
    assert fake["native"] == [["de.cais.send_to_tv", {"action": "status"}]]
    assert fake["updates"] == [["send-to-tv-link", state]]
    assert fake["refreshes"] == 1


def test_other_menus_ask_nothing():
    fake = bg('await fake.listeners.shown({menuIds: ["other"]});')
    assert fake["native"] == [] and fake["updates"] == [] and fake["refreshes"] == 0


def test_a_closed_menu_is_left_alone():
    fake = bg("""let answer;
        fake.replies = [() => new Promise((resolve) => { answer = resolve; })];
        const shown = fake.listeners.shown({menuIds: ["send-to-tv-page"]});
        await new Promise((r) => setTimeout(r, 0));
        fake.listeners.hidden();
        answer({connected: true, name: "Living Room", links: true});
        await shown;""")
    assert len(fake["native"]) == 1
    assert fake["updates"] == [] and fake["refreshes"] == 0


# ---- sending ----

def click(target, reply, **extra):
    """A click on target (link menu, page menu, page action) with the host's reply."""
    replies = json.dumps([reply] if reply is not None else [])
    flags = "".join(f"fake.{k} = {json.dumps(v)};\n" for k, v in extra.items())
    calls = {
        "link": f'fake.listeners.clicked({{menuItemId: "send-to-tv-link", linkUrl: "{VIDEO}", '
                'pageUrl: "https://www.youtube.com/"}, {id: 7, url: "https://www.youtube.com/"})',
        "page": f'fake.listeners.clicked({{menuItemId: "send-to-tv-page", pageUrl: "{VIDEO}"}}, '
                '{id: 7, url: "https://example.org/"})',
        "page-no-url": f'fake.listeners.clicked({{menuItemId: "send-to-tv-page"}}, '
                       f'{{id: 7, url: "{VIDEO}"}})',
        "action": f'fake.listeners.pageAction({{id: 7, url: "{VIDEO}"}})',
    }
    return bg(f"fake.replies = {replies};\n{flags}await {calls[target]};")


@pytest.mark.parametrize("target", ["link", "page", "page-no-url", "action"])
def test_a_click_plays_the_video_and_pauses_the_browser(target):
    fake = click(target, PLAYING)
    assert fake["native"] == [["de.cais.send_to_tv", {"action": "play", "url": VIDEO}]]
    assert fake["scripts"] == [7]
    assert fake["notifications"] == ["playing(Living Room)"]


@pytest.mark.parametrize("result,text", [("starting", "starting(Living Room)"),
                                         ("code", "code(Living Room)")])
def test_a_tv_still_starting_or_asking_for_a_code(result, text):
    fake = click("action", {"result": result, "name": "Living Room"})
    assert fake["notifications"] == [text] and fake["scripts"] == [7]


def test_a_tv_without_a_name():
    fake = click("action", {"result": "playing", "name": ""})
    assert fake["notifications"] == ["playing(theTv)"]


def test_a_page_the_add_on_may_not_touch_still_plays():
    fake = click("action", PLAYING, scriptFails=True)
    assert fake["scripts"] == [7] and fake["notifications"] == ["playing(Living Room)"]


@pytest.mark.parametrize("tab", ["undefined", "{url: 'X'}", "{id: -1, url: 'X'}"])
def test_no_tab_to_pause(tab):
    fake = bg(f"""fake.replies = [{json.dumps(PLAYING)}];
        await send({json.dumps(VIDEO)}, {tab});""")
    assert fake["scripts"] == [] and fake["notifications"] == ["playing(Living Room)"]


@pytest.mark.parametrize("reply,text", [
    ({"result": "error", "error": "no-tv", "name": "", "detail": ""}, "noTv"),
    ({"result": "error", "error": "no-links", "name": "Living Room", "detail": ""},
     "noLinks(Living Room)"),
    ({"result": "error", "error": "bad-url", "name": "", "detail": ""}, "notAVideo"),
    ({"result": "error", "error": "failed", "name": "Living Room", "detail": "TV off"},
     "failed(TV off)"),
    ({"result": "error", "error": "failed", "name": "", "detail": ""}, "failed(unknownError)"),
    ({"result": "error", "error": "new-in-a-later-host", "name": ""}, "failed(unknownError)"),
    ({"result": "something-else"}, "failed(unknownError)"),
    (None, "noHost"),
])
def test_what_goes_wrong_is_said(reply, text):
    fake = click("action", reply)
    assert fake["notifications"] == [text]
    assert fake["scripts"] == []


def test_a_link_that_is_no_video_is_not_sent():
    fake = bg('await fake.listeners.pageAction({id: 7, url: "https://www.youtube.com/"});')
    assert fake["native"] == [] and fake["notifications"] == ["notAVideo"]


def test_clicks_on_other_menus_are_ignored():
    fake = bg('return [await fake.listeners.clicked({menuItemId: "other", linkUrl: "X"}, {id: 1}), '
              'fake.native.length];')
    assert fake == [None, 0]


# ---- the files agree with each other ----

def manifest():
    return json.loads((PKGDIR / "manifest.json").read_text())


def pkgbuild_var(name):
    m = re.search(rf"^{name}=(\S+)$", (PKGDIR / "PKGBUILD").read_text(), re.M)
    return m.group(1)


def test_versions_and_ids_agree():
    m = manifest()
    native = json.loads((PKGDIR / "de.cais.send_to_tv.json").read_text())
    addon_id = m["browser_specific_settings"]["gecko"]["id"]
    assert m["version"] == pkgbuild_var("_xpiver")
    assert native["allowed_extensions"] == [addon_id]
    assert f'const HOST = "{native["name"]}";' in BACKGROUND.read_text()
    # The manifest's file name is its name, as Firefox looks it up
    assert native["name"] == "de.cais.send_to_tv" and native["type"] == "stdio"
    pkgbuild = (PKGDIR / "PKGBUILD").read_text()
    assert native["path"] == "/usr/lib/plinius-send-to-tv/send-to-tv-host"
    assert 'install -Dm755 send-to-tv-host -t "$pkgdir/usr/lib/plinius-send-to-tv"' in pkgbuild
    assert f'"$pkgdir/usr/lib/firefox/browser/extensions/{addon_id}.xpi"' in pkgbuild
    # mobile-config-firefox forces it on from that very path
    policy = (PKG / "mobile-config-firefox" / "PKGBUILD").read_text()
    assert f'"{addon_id}": "/usr/lib/firefox/browser/extensions",' in policy


def test_the_address_bar_button_shows_where_the_menu_does():
    patterns = bg("return LINK_PATTERNS;")
    assert set(manifest()["page_action"]["show_matches"]) == set(patterns) - {
        "*://www.youtube.com/embed/*"}       # an embedded player is no page of its own


def test_every_string_is_there_and_used(mkxpi):
    template = mkxpi.read_po(PKGDIR / "send-to-tv.pot")
    text = BACKGROUND.read_text() + (PKGDIR / "manifest.json").read_text()
    used = set(re.findall(r'msg\("(\w+)"', text)) | set(re.findall(r"__MSG_(\w+)__", text))
    # The results with a text of their own (send): {result: key}
    (table,) = re.findall(r"const texts = \{([^}]*)\}", text)
    used |= set(re.findall(r'"(\w+)"', table))
    assert used == set(template)


@pytest.mark.parametrize("lang", LANGS)
def test_translations_are_complete(mkxpi, lang):
    template = mkxpi.read_po(PKGDIR / "send-to-tv.pot")
    po = mkxpi.read_po(PKGDIR / f"{lang}.po")
    assert set(po) == set(template)
    for key, (en, _) in template.items():
        have_en, translated = po[key]
        assert have_en == en, f"{lang}.po: {key} has an old English text"
        assert translated, f"{lang}.po: {key} untranslated"
        assert translated.count("$1") == en.count("$1"), f"{lang}.po: {key} loses $1"
    assert set(mkxpi.messages(PKGDIR)[lang]) == set(template)


def test_the_package_lists_every_translation():
    pkgbuild = (PKGDIR / "PKGBUILD").read_text()
    assert "de.po es.po fr.po it.po nl.po pl.po pt.po ru.po" in pkgbuild


# ---- make-xpi.py ----

def sources(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name in ("manifest.json", "background.js", "send-to-tv.svg", "send-to-tv.pot",
                 *(f"{lang}.po" for lang in LANGS)):
        shutil.copy(PKGDIR / name, src / name)
    return src


def sign(unsigned, signed, change=None, extra=None, drop=None):
    """What AMO does: the same files plus META-INF/; optionally one changed, added or left out."""
    with zipfile.ZipFile(unsigned) as a, zipfile.ZipFile(signed, "w") as b:
        for name in a.namelist():
            if name == drop:
                continue
            data = a.read(name)
            b.writestr(name, data + b"//" if name == change else data)
        if extra:
            b.writestr(extra, b"x")
        b.writestr("META-INF/", b"")
        b.writestr("META-INF/cose.sig", b"sig")


def test_build_is_reproducible_and_complete(mkxpi, tmp_path):
    a, b = tmp_path / "a.xpi", tmp_path / "b.xpi"
    mkxpi.build(PKGDIR, a)
    mkxpi.build(PKGDIR, b)
    assert a.read_bytes() == b.read_bytes()
    with zipfile.ZipFile(a) as z:
        names = z.namelist()
        en = json.loads(z.read("_locales/en/messages.json"))
        de = json.loads(z.read("_locales/de/messages.json"))
    assert set(names) == {"manifest.json", "background.js", "send-to-tv.svg",
                          *(f"_locales/{lang}/messages.json" for lang in ("en", *LANGS))}
    assert en["name"] == {"message": "Send to TV"}
    assert de["playOn"] == {"message": "Auf „$1“ abspielen"}


def test_check_accepts_the_signed_copy_of_these_sources(mkxpi, tmp_path):
    mkxpi.build(PKGDIR, tmp_path / "u.xpi")
    sign(tmp_path / "u.xpi", tmp_path / "s.xpi")
    assert mkxpi.check(PKGDIR, tmp_path / "s.xpi") == []
    assert mkxpi.main(["make-xpi.py", "--check", str(PKGDIR), str(tmp_path / "s.xpi")]) == 0


def test_check_finds_every_difference(mkxpi, tmp_path, capsys):
    mkxpi.build(PKGDIR, tmp_path / "u.xpi")
    assert mkxpi.check(PKGDIR, tmp_path / "u.xpi") == ["no signature (META-INF/) in the add-on"]
    sign(tmp_path / "u.xpi", tmp_path / "s.xpi", change="background.js", extra="evil.js",
         drop="_locales/ru/messages.json")
    assert mkxpi.check(PKGDIR, tmp_path / "s.xpi") == [
        "missing in the add-on: _locales/ru/messages.json",
        "differs from the sources: background.js",
        "not from the sources: evil.js"]
    assert mkxpi.main(["make-xpi.py", "--check", str(PKGDIR), str(tmp_path / "s.xpi")]) == 1
    assert "differs from the sources: background.js" in capsys.readouterr().err


def test_main_builds_and_explains_its_arguments(mkxpi, tmp_path, capsys):
    out = tmp_path / "o.xpi"
    assert mkxpi.main(["make-xpi.py", str(PKGDIR), str(out)]) == 0
    assert zipfile.is_zipfile(out)
    assert mkxpi.main(["make-xpi.py"]) == 2
    assert "usage" in capsys.readouterr().err


def test_the_package_checks_the_signed_copy():
    pkgbuild = (PKGDIR / "PKGBUILD").read_text()
    assert 'python3 make-xpi.py --check . "send-to-tv-$_xpiver.xpi"' in pkgbuild


def test_too_long_texts_stop_the_build(mkxpi, tmp_path):
    src = sources(tmp_path)
    po = src / "de.po"
    po.write_text(po.read_text().replace("An Fernseher senden", "A" * 46))
    with pytest.raises(ValueError, match="de: name longer than 45"):
        mkxpi.build(src, tmp_path / "x.xpi")
    assert not (tmp_path / "x.xpi").exists()


def test_old_fuzzy_or_missing_translations_fall_back_to_english(mkxpi, tmp_path):
    src = sources(tmp_path)
    po = src / "fr.po"
    text = po.read_text()
    text = text.replace('msgctxt "name"', '#, fuzzy\nmsgctxt "name"')
    text = text.replace('msgid "Play on TV"', 'msgid "Play on the TV"')
    text = text.replace('msgstr "erreur inconnue"', 'msgstr ""')
    po.write_text(text)
    fr = mkxpi.messages(src)["fr"]
    assert "name" not in fr and "playOnTv" not in fr and "unknownError" not in fr
    assert fr["playing"] == {"message": "Lecture sur « $1 »."}


def test_po_with_continued_lines_and_escapes(mkxpi, tmp_path):
    po = tmp_path / "x.po"
    po.write_text('msgid ""\nmsgstr ""\n"Language: x\\n"\n\n'
                  '# comment\nmsgctxt "a"\nmsgid "one "\n"two"\nmsgstr "eins \\"zwei\\"\\t"\n'
                  'msgctxt "b"\nmsgid "b"\nmsgstr "c\\\\"\n')
    assert mkxpi.read_po(po) == {"a": ("one two", 'eins "zwei"\t'), "b": ("b", "c\\")}


@pytest.mark.parametrize("text,error", [
    ('msgctxt "a"\nmsgid "a"\nmsgstr "a"\n\nmsgctxt "a"\nmsgid "a"\nmsgstr "b"\n', "key a twice"),
    ('"lost"\n', "continuation without a field"),
    ('msgid_plural "x"\n', "unknown line"),
    ("msgid x\n", "not a quoted string"),
])
def test_po_errors(mkxpi, tmp_path, text, error):
    po = tmp_path / "x.po"
    po.write_text(text)
    with pytest.raises(ValueError, match=error):
        mkxpi.read_po(po)


def test_keys_missing_from_the_template_are_refused(mkxpi, tmp_path):
    src = sources(tmp_path)
    with open(src / "it.po", "a") as f:
        f.write('\nmsgctxt "gone"\nmsgid "Gone"\nmsgstr "Andato"\n')
    with pytest.raises(ValueError, match=r"it.po: keys not in the template: \['gone'\]"):
        mkxpi.messages(src)
