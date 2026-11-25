# Speech Output (cc-speech-panel.c): the voice of the system language (org.plinius.speech voices),
# the speech rate and the caller announcement. The voices lie in the program's root as
# plinius-speech installs them (voices/<id>/voice.json) and its index voices.json names the ones
# to download; plinius-speechd is a fake on the test's session bus with the interface of
# pkgbuilds/plinius-speech/plinius-speechd.c; downloading and removing go through a fake
# "pkexec plinius-pkg" with the progress from the package file growing in pacman's cache, as on
# the Keyboard page.

import json
import shutil
import subprocess

import pytest

from gcc_harness import (PKG, build_panel, call, click, dump, gcc_session, gcc_world,  # noqa: F401
                         interface_xml, set_active, settle, wait, wait_for)

SPEECH = "org.plinius.Speech1"
SPEECH_PATH = "/org/plinius/Speech1"
SCHEMA = "org.plinius.speech"
DATA = "/usr/share/plinius-speech"
PKG_TOOL = "/usr/lib/plinius-software/plinius-pkg"
CACHE = "/var/cache/pacman/pkg"
SAMPLE = "Daniel Müller is calling"

GLUE = r'''
#include "gcc_driver.h"
#include "cc-speech-panel.h"

GType gcc_test_panel_get_type (void);

CcPanel *
gcc_test_create_panel (GccTest *t)
{
    /* Without plinius-speech's schema the panel cannot be made: only the listing is asked */
    if (!g_strcmp0 (gcc_test_param (t, "panel"), "none"))
        return g_object_new (gcc_test_panel_get_type (), "window", t->window, NULL);
    return g_object_new (CC_TYPE_SPEECH_PANEL, "window", t->window, NULL);
}

void
gcc_test_setup (GccTest *t)
{
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (!g_str_equal (name, "static_init"))
        return FALSE;
    cc_speech_panel_static_init_func ();
    json_builder_add_null_value (result);
    return TRUE;
}
'''

MB63 = 63221984


def voice(vid, name, locale, size=MB63, default=False, package=None):
    v = {"id": vid, "name": name, "language": locale[:2], "locale": locale,
         "package": package or f"plinius-speech-voice-{locale.lower().replace('_', '-')}-{name.lower()}",
         "size": size, "license": "CC0-1.0"}
    if default:
        v["default"] = True
    return v


THORSTEN = voice("de_DE-thorsten-medium", "Thorsten", "de_DE", default=True)
KERSTIN = voice("de_DE-kerstin-low", "Kerstin", "de_DE", size=20000000)
JOE = voice("en_US-joe-medium", "Joe", "en_US", default=True)
KATHLEEN = voice("en_US-kathleen-low", "Kathleen", "en_US", size=20000000)
CORI = voice("en_GB-cori-medium", "Cori", "en_GB", default=True)
PIM = voice("nl_NL-pim-medium", "Pim", "nl_NL", default=True)
NATHALIE = voice("nl_BE-nathalie-medium", "Nathalie", "nl_BE", default=True)
TUGAO = voice("pt_PT-tugão-medium", "Tugão", "pt_PT", default=True, package="plinius-speech-voice-pt-pt-tugao")
FABER = voice("pt_BR-faber-medium", "Faber", "pt_BR", default=True)
INDEX = [THORSTEN, KERSTIN, JOE, KATHLEEN, CORI, PIM, NATHALIE, TUGAO, FABER]


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    reps = [(f'"{p}', f'"{{root}}{p}') for p in (DATA, PKG_TOOL, CACHE)]
    return build_panel(gcc_session, tmp_path_factory.mktemp("speech"), "speech", ["cc-speech-panel.c"], GLUE,
                       replacements={"cc-speech-panel.c": reps})


@pytest.fixture(scope="module")
def no_speech_schemas(gcc_session, tmp_path_factory):
    """The session's schemas without org.plinius.speech"""
    d = tmp_path_factory.mktemp("speech-schemas")
    for f in gcc_session.schemas.glob("*.gschema.xml"):
        if f.name != f"{SCHEMA}.gschema.xml":
            shutil.copy(f, d / f.name)
    for f in gcc_session.schemas.glob("*.enums.xml"):
        shutil.copy(f, d / f.name)
    subprocess.run(["glib-compile-schemas", str(d)], check=True, capture_output=True)
    return d


class Phone:
    def __init__(self, program, world):
        self.root = program.root
        self.world = world
        self.bin = world.fake_bin
        for d in list(self.root.iterdir()):
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        self.language("de_DE.UTF-8")

    def path(self, p):
        return self.root / p.lstrip("/")

    def language(self, value):
        """The language GLib names first (LANGUAGE wins over LC_ALL there)"""
        if value is None:
            self.world.extra_env.pop("LANGUAGE", None)
        else:
            self.world.extra_env["LANGUAGE"] = value

    def index(self, voices=INDEX, text=None):
        f = self.path(f"{DATA}/voices.json")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text if text is not None else json.dumps(voices))

    def voice_dir(self, vid):
        return self.path(f"{DATA}/voices/{vid}")

    def install(self, voice, **changes):
        d = self.voice_dir(changes.pop("dir", voice["id"]))
        d.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in voice.items() if k not in ("package", "size")}
        data.update({"model": f"{voice['id']}.onnx", "config": f"{voice['id']}.onnx.json", "quality": "medium"})
        data.update(changes)
        (d / "voice.json").write_text(json.dumps(data))

    def speechd(self, **replies):
        xml = interface_xml(PKG / "plinius-speech" / "plinius-speechd.c", SPEECH)
        self.world.service("session", SPEECH, SPEECH_PATH, xml, replies=replies,
                           properties={"Language": "'de'", "Voice": "''"})

    def pacman(self, size_line="Download Size   : 1.50 MiB"):
        out = f"Name            : plinius-speech-voice-de-de-kerstin\\n{size_line}\\nInstalled Size  : 19.00 MiB\\n"
        self.bin.stub("pacman", f'[ "$LC_ALL" = C ] || exit 7\nprintf "{out}"')

    def pkexec(self, code=0, output="", voice=None, wait_for=None):
        """pkexec plinius-pkg: waits for the file wait_for, installs or removes the voice's
        directory, exits with code."""
        lines = []
        if wait_for:
            lines.append(f'for i in $(seq 1 400); do [ -e "{wait_for}" ] && break; sleep 0.05; done')
        if output:
            lines.append(f'printf "%s" "{output}"')
        if voice:
            d = self.voice_dir(voice["id"])
            data = json.dumps({"id": voice["id"], "name": voice["name"], "language": voice["language"]})
            lines.append(f"if [ \"$2\" = install ]; then mkdir -p '{d}'; printf '%s' '{data}' > '{d}/voice.json'; fi")
            lines.append(f"if [ \"$2\" = remove ]; then rm -rf '{d}'; fi")
        lines.append(f"exit {code}")
        self.bin.stub("pkexec", "\n".join(lines))

    def pkexec_calls(self):
        return self.bin.calls("pkexec")


@pytest.fixture
def phone(program, gcc_world):
    return Phone(program, gcc_world)


def get(key, label=None):
    return {"op": "gsettings_get", "schema": SCHEMA, "key": key, "label": label or key}


def put(key, value):
    return {"op": "gsettings_set", "schema": SCHEMA, "key": key, "value": value}


def voice_group(s):
    return s.within(type="AdwPreferencesGroup", text="Voice")


def row(s, name):
    return s.one(type="AdwActionRow", text=name)


def buttons(node):
    return [(n.get("icon"), n.get("tooltip")) for n in node.walk() if n["type"] == "GtkButton"]


def checked(node):
    return [n.get("active") for n in node.walk() if n["type"] == "GtkCheckButton"]


def chosen(s):
    """The voice rows whose check button is on"""
    return [r["text"] for r in voice_group(s).find(type="AdwActionRow") if checked(r) == [True]]


def listen(name):
    return click(type="GtkButton", tooltip="Listen", within={"type": "AdwActionRow", "text": name})


def stop_button(name):
    return click(type="GtkButton", tooltip="Stop", within={"type": "AdwActionRow", "text": name})


def messages(r):
    return [m["message"] for m in r.warnings()]


# ----- The listing -----

@pytest.mark.parametrize("with_schema, shown", [(True, "visible"), (False, "hidden")])
def test_listed_only_with_plinius_speech(program, gcc_world, phone, no_speech_schemas, with_schema, shown):
    if not with_schema:
        gcc_world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(no_speech_schemas)
    r = gcc_world.run(program, [call("static_init")], params={} if with_schema else {"panel": "none"})
    assert r.visibility == {"speech": shown}


# ----- The page -----

def test_page_with_the_voices_of_the_system_language(program, gcc_world, phone):
    phone.language("en_US.UTF-8")
    phone.index()
    phone.install(JOE)
    phone.install(KATHLEEN)
    phone.install(THORSTEN)
    r = gcc_world.run(program, [dump("main"), get("voices"), get("rate"), get("announce-callers")])
    s = r.dump("main")
    assert [g["text"] for g in s.find(type="AdwPreferencesGroup")] == ["Voice", "Speech Rate", "Announcements"]
    group = s.one(type="AdwPreferencesGroup", text="Voice")
    assert group["subtitle"] == ("Language: English. The phone always speaks the language set in "
                                 "Region & Language.")
    # Only English voices, the system's locale first; two locales: each one names its region.
    # Thorsten (German) is installed but not listed
    joe, kathleen, cori = "Joe (United States)", "Kathleen (United States)", "Cori (United Kingdom)"
    assert [n["text"] for n in voice_group(s).find(type="AdwActionRow")] == [joe, kathleen, cori]
    # Nothing chosen: the default voice of en_US speaks
    assert chosen(s) == [joe]
    assert row(s, joe)["subtitle"] == "Standard voice"
    assert buttons(row(s, joe)) == [("media-playback-start-symbolic", "Listen")]
    # Another installed voice can be removed, the one that speaks cannot
    assert not row(s, kathleen).get("subtitle")
    assert buttons(row(s, kathleen)) == [("media-playback-start-symbolic", "Listen"),
                                         ("user-trash-symbolic", "Remove")]
    assert row(s, cori)["subtitle"] == "Not installed, 63.2\xa0MB"
    assert buttons(row(s, cori)) == [("folder-download-symbolic", "Download")]
    assert checked(row(s, cori)) == []
    rate = s.one(type="AdwPreferencesGroup", text="Speech Rate")
    assert rate["subtitle"] == "The voice and the speech rate apply to everything the phone says."
    labels = [n.get("text") for n in rate.walk() if n["type"] == "GtkLabel"]
    assert [t for t in labels if t in ("Slower", "Normal", "Faster")] == ["Slower", "Normal", "Faster"]
    sw = s.one(type="AdwSwitchRow", text="Announce Callers")
    assert sw["active"] is True
    assert sw["subtitle"] == ("In a Bluetooth headset or headphones, the phone says who is calling after the "
                              "first ring: the name from the contacts, or “Unknown caller”. Not in the car.")
    assert not s.has(type="AdwActionRow", text="Downloading Voice…")
    assert r.value("voices") == "{}"
    assert r.value("rate") == "1.2"
    assert not r.warnings()


EN_C = ["Joe (United States)", "Kathleen (United States)", "Cori (United Kingdom)"]


@pytest.mark.parametrize("language, name, expect", [
    (None, "English", EN_C),                # LC_ALL=C.UTF-8 of the test: "C" is English, no region
    ("en_GB", "English", ["Cori (United Kingdom)", "Joe (United States)", "Kathleen (United States)"]),
    ("POSIX", "English", EN_C),
    ("@euro", "English", EN_C),             # nothing before the modifier
    ("de", "German", ["Thorsten", "Kerstin"]),   # one locale: no regions
    ("RU", "Russian", []),                  # the code in lower case
    ("sv_SE.UTF-8", "sv", []),              # a language we have no name for: its code
])
def test_the_language_decides_which_voices(program, gcc_world, phone, language, name, expect):
    phone.language(language)
    phone.index()
    phone.install(JOE)
    phone.install(THORSTEN)
    r = gcc_world.run(program, [dump("main")])
    s = r.dump("main")
    assert s.one(type="AdwPreferencesGroup", text="Voice")["subtitle"].startswith(f"Language: {name}. ")
    rows = [n["text"] for n in voice_group(s).find(type="AdwActionRow")]
    assert rows == (expect or ["No Voice Available"])


@pytest.mark.parametrize("language, installed, expect", [
    ("pt_BR.UTF-8", [TUGAO, FABER], "Faber (Brazil)"),        # the default of the system's locale
    ("pt_PT.UTF-8", [TUGAO, FABER], "Tugão (Portugal)"),
    ("pt", [FABER, TUGAO], "Tugão (Portugal)"),               # no region: the first default voice
    ("nl_BE", [PIM, NATHALIE], "Nathalie (Belgium)"),
    ("en_US", [KATHLEEN, CORI], "Kathleen (United States)"),  # any voice of the locale before others
    ("en_AU", [KATHLEEN, CORI], "Cori (United Kingdom)"),     # no voice of en_AU: a default voice
    ("en_AU", [KATHLEEN], "Kathleen (United States)"),        # else the first
])
def test_the_locale_decides_the_voice_that_speaks(program, gcc_world, phone, language, installed, expect):
    phone.language(language)
    phone.index()
    for v in installed:
        phone.install(v)
    r = gcc_world.run(program, [dump("main")])
    assert chosen(r.dump("main")) == [expect]


def test_regions_without_a_name(program, gcc_world, phone):
    phone.language("en_US.UTF-8")
    phone.index([JOE])
    phone.install({"id": "en_IE-aoife", "name": "Aoife", "language": "en", "locale": "en_IE"})
    phone.install({"id": "en-plain", "name": "Plain", "language": "en", "locale": "en"})
    phone.install({"id": "en-none", "name": "Nolocale & Co", "language": "en"})
    r = gcc_world.run(program, [dump("main")])
    # Installed voices outside the index after it, by id; a name is no markup
    assert [n["text"] for n in voice_group(r.dump("main")).find(type="AdwActionRow")] == [
        "Joe (United States)", "Nolocale & Co", "Plain (en)", "Aoife (IE)"]
    assert not r.warnings()


def test_no_voice_for_the_language(program, gcc_world, phone):
    phone.index([JOE])
    r = gcc_world.run(program, [dump("main")])
    s = r.dump("main")
    none = row(s, "No Voice Available")
    assert none["subtitle"] == "There is no voice for this language yet, so the phone says nothing."
    assert buttons(none) == [] and checked(none) == []
    # Without the index and without voices the same
    phone.index(text="[]")
    assert gcc_world.run(program, [dump("main")]).dump("main").has(type="AdwActionRow", text="No Voice Available")
    assert not r.warnings()


def test_without_the_index_only_the_installed_voices(program, gcc_world, phone):
    phone.install(KERSTIN)
    r = gcc_world.run(program, [dump("main")])
    s = r.dump("main")
    assert [n["text"] for n in voice_group(s).find(type="AdwActionRow")] == ["Kerstin"]
    # From no known package: nothing to remove; the only voice speaks
    assert buttons(row(s, "Kerstin")) == [("media-playback-start-symbolic", "Listen")]
    assert chosen(s) == ["Kerstin"]
    assert not r.warnings()


def test_broken_entries_are_left_out(program, gcc_world, phone):
    phone.index([
        "not an object", {"name": "No Id", "language": "de"}, {"id": "a/b", "language": "de", "package": "p"},
        {"id": "..", "language": "de", "package": "p"}, {"id": ".", "language": "de", "package": "p"},
        {"id": "de-noname", "language": "de", "package": "plinius-speech-voice-de-noname", "size": "big",
         "default": "yes"},
        {"id": "de-noname", "language": "de", "name": "Twice", "package": "x"},
        {"id": 7, "language": "de"},
        # Neither installed nor from a package: nothing to show
        {"id": "de-nopkg", "language": "de", "name": "Nowhere"},
    ])
    # Installed: a broken file, one naming another id, one of another language, a directory
    # without voice.json, one without a name, one that is no object
    d = phone.voice_dir("broken")
    d.mkdir(parents=True)
    (d / "voice.json").write_text("{")
    phone.install(KERSTIN, dir="elsewhere")
    phone.install(JOE)
    phone.voice_dir("empty").mkdir()
    phone.install({"id": "de-plain", "language": "de"})
    lst = phone.voice_dir("list")
    lst.mkdir()
    (lst / "voice.json").write_text("[]")
    r = gcc_world.run(program, [dump("main")])
    s = r.dump("main")
    assert [n["text"] for n in voice_group(s).find(type="AdwActionRow")] == ["de-noname", "de-plain"]
    # A size that is no number: none shown; "default" that is no boolean: no default
    assert row(s, "de-noname")["subtitle"] == "Not installed"
    assert not row(s, "de-plain").get("subtitle")
    assert chosen(s) == ["de-plain"]
    assert len([m for m in messages(r) if m.startswith("Cannot read ") and "broken/voice.json" in m]) == 1


def test_a_broken_index_is_said_and_left_out(program, gcc_world, phone):
    phone.index(text="{not json")
    phone.install(THORSTEN)
    r = gcc_world.run(program, [dump("main")])
    assert [n["text"] for n in voice_group(r.dump("main")).find(type="AdwActionRow")] == ["Thorsten"]
    assert [m for m in messages(r) if m.startswith("Cannot read ") and "voices.json" in m]
    # An index that is no list says nothing
    phone.index(text='{"id": "x"}')
    r = gcc_world.run(program, [dump("main")])
    assert not r.warnings()


# ----- Choosing a voice -----

def test_choosing_a_voice_writes_it_for_the_language(program, gcc_world, phone):
    phone.index()
    phone.install(THORSTEN)
    phone.install(KERSTIN)
    r = gcc_world.run(program, [put("voices", "{'en': 'en_US-joe-medium', 'de': 'de_DE-thorsten-medium'}"),
                                settle(),
                                set_active(True, type="GtkCheckButton", within={"type": "AdwActionRow",
                                                                                "text": "Kerstin"}),
                                settle(), get("voices"), dump("main")])
    # The other language's choice stays
    assert r.value("voices") == "{'en': 'en_US-joe-medium', 'de': 'de_DE-kerstin-low'}"
    s = r.dump("main")
    assert chosen(s) == ["Kerstin"]
    # Now Thorsten can be removed, Kerstin not
    assert ("user-trash-symbolic", "Remove") in buttons(row(s, "Thorsten"))
    assert ("user-trash-symbolic", "Remove") not in buttons(row(s, "Kerstin"))


def test_activating_the_row_chooses_too(program, gcc_world, phone):
    phone.index()
    phone.install(THORSTEN)
    phone.install(KERSTIN)
    r = gcc_world.run(program, [click(type="AdwActionRow", text="Kerstin"), settle(), get("voices")])
    assert r.value("voices") == "{'de': 'de_DE-kerstin-low'}"


@pytest.mark.parametrize("voices, expect", [
    ("{'de': 'de_DE-kerstin-low'}", "Kerstin"),
    ("{'de': 'gone'}", "Thorsten"),                 # chosen, but not there: the default speaks
    ("{'de': 'en_US-joe-medium'}", "Thorsten"),     # a voice of another language counts not
    ("{'en': 'de_DE-kerstin-low'}", "Thorsten"),    # another language's choice counts not
])
def test_the_voice_that_speaks(program, gcc_world, phone, voices, expect):
    phone.index()
    phone.install(THORSTEN)
    phone.install(KERSTIN)
    phone.install(JOE)
    r = gcc_world.run(program, [put("voices", voices), settle(), dump("main")])
    assert chosen(r.dump("main")) == [expect]


def test_chosen_but_removed_meanwhile(program, gcc_world, phone):
    phone.index()
    phone.install(THORSTEN)
    r = gcc_world.run(program, [put("voices", "{'de': 'de_DE-kerstin-low'}"), settle(), dump("main")])
    # Kerstin is in the index but not installed: Thorsten speaks
    assert chosen(r.dump("main")) == ["Thorsten"]


# ----- Rate and announcement -----

def test_the_rate_slider_is_the_setting(program, gcc_world, phone):
    phone.index()
    r = gcc_world.run(program, [{"op": "set_value", "target": {"type": "GtkScale"}, "value": 1.5}, settle(),
                                get("rate", "faster"),
                                {"op": "set_value", "target": {"type": "GtkScale"}, "value": 0.1}, settle(),
                                get("rate", "lowest"),
                                {"op": "set_value", "target": {"type": "GtkScale"}, "value": 3.0}, settle(),
                                get("rate", "highest")])
    assert r.value("faster") == "1.5"
    assert r.value("lowest") == "0.5"
    assert r.value("highest") == "2.0"


def test_the_switch_is_the_setting(program, gcc_world, phone):
    phone.index()
    r = gcc_world.run(program, [set_active(False, type="AdwSwitchRow"), settle(), get("announce-callers", "off"),
                                put("announce-callers", "true"), settle(), dump("on")])
    assert r.value("off") == "false"
    assert r.dump("on").one(type="AdwSwitchRow", text="Announce Callers")["active"] is True


# ----- Listening -----

def speaks(world):
    return [c["args"] for c in world.calls(SPEECH, "Speak")]


def stops(world):
    return [c["args"] for c in world.calls(SPEECH, "Stop")]


@pytest.fixture
def voices(phone):
    phone.index()
    phone.install(THORSTEN)
    phone.install(KERSTIN)
    return phone


def test_listen_speaks_the_sample_until_finished(program, gcc_world, voices):
    voices.speechd(Speak="(uint32 5,)")
    ctl = gcc_world.control("session")
    r = gcc_world.run(program, [listen("Kerstin"), settle(200), dump("playing"),
                                # Another sample's end and a malformed signal change nothing
                                ctl.emit(SPEECH_PATH, SPEECH, "Finished", "(uint32 4, true)"), settle(),
                                ctl.emit_raw(SPEECH_PATH, SPEECH, "Finished", "(uint32 5,)"), settle(),
                                dump("still"),
                                ctl.emit(SPEECH_PATH, SPEECH, "Finished", "(uint32 5, true)"), settle(),
                                dump("done")])
    assert speaks(gcc_world) == [[SAMPLE, {"voice": "de_DE-kerstin-low"}]]
    playing = r.dump("playing")
    assert buttons(row(playing, "Kerstin"))[0] == ("media-playback-stop-symbolic", "Stop")
    assert buttons(row(playing, "Thorsten")) == [("media-playback-start-symbolic", "Listen")]
    assert buttons(row(r.dump("still"), "Kerstin"))[0] == ("media-playback-stop-symbolic", "Stop")
    assert buttons(row(r.dump("done"), "Kerstin"))[0] == ("media-playback-start-symbolic", "Listen")
    assert stops(gcc_world) == []
    assert not r.warnings()


def test_the_same_button_stops_it(program, gcc_world, voices):
    voices.speechd(Speak="(uint32 6,)")
    r = gcc_world.run(program, [listen("Thorsten"), settle(200), stop_button("Thorsten"), settle(200),
                                dump("stopped")])
    assert stops(gcc_world) == [[6]]
    assert buttons(row(r.dump("stopped"), "Thorsten")) == [("media-playback-start-symbolic", "Listen")]


def test_another_voice_stops_the_first(program, gcc_world, voices):
    voices.speechd(Speak=["(uint32 7,)", "(uint32 8,)"])
    r = gcc_world.run(program, [listen("Thorsten"), settle(200), listen("Kerstin"), settle(200), dump("second")])
    assert speaks(gcc_world) == [[SAMPLE, {"voice": "de_DE-thorsten-medium"}],
                                 [SAMPLE, {"voice": "de_DE-kerstin-low"}]]
    # The first when the second started, the second when the page closed
    assert stops(gcc_world) == [[7], [8]]
    s = r.dump("second")
    assert buttons(row(s, "Thorsten"))[0][1] == "Listen"
    assert buttons(row(s, "Kerstin"))[0][1] == "Stop"


def test_stopped_before_the_answer(program, gcc_world, voices):
    voices.speechd(Speak={"delay_ms": 400, "reply": "(uint32 11,)"})
    r = gcc_world.run(program, [listen("Thorsten"), stop_button("Thorsten"), settle(10), dump("stopped"),
                                wait(700), settle()])
    assert buttons(row(r.dump("stopped"), "Thorsten"))[0][1] == "Listen"
    # The late answer's sample is stopped at once
    assert stops(gcc_world) == [[11]]


def test_finished_before_the_answer(program, gcc_world, voices):
    voices.speechd(Speak={"delay_ms": 400, "reply": "(uint32 12,)"})
    ctl = gcc_world.control("session")
    r = gcc_world.run(program, [listen("Kerstin"), settle(10),
                                ctl.emit(SPEECH_PATH, SPEECH, "Finished", "(uint32 12, true)"), settle(10),
                                dump("waiting"), wait(700), settle(), dump("done")])
    assert buttons(row(r.dump("waiting"), "Kerstin"))[0][1] == "Stop"
    assert buttons(row(r.dump("done"), "Kerstin"))[0][1] == "Listen"
    assert stops(gcc_world) == []


def test_an_earlier_end_does_not_end_the_next(program, gcc_world, voices):
    voices.speechd(Speak={"delay_ms": 400, "reply": "(uint32 13,)"})
    ctl = gcc_world.control("session")
    r = gcc_world.run(program, [listen("Kerstin"), settle(10),
                                ctl.emit(SPEECH_PATH, SPEECH, "Finished", "(uint32 3, false)"), settle(10),
                                wait(700), settle(), dump("playing"),
                                ctl.emit(SPEECH_PATH, SPEECH, "Finished", "(uint32 13, false)"), settle(),
                                dump("done")])
    assert buttons(row(r.dump("playing"), "Kerstin"))[0][1] == "Stop"
    assert buttons(row(r.dump("done"), "Kerstin"))[0][1] == "Listen"


def test_the_service_refusing_is_said(program, gcc_world, voices):
    voices.speechd(Speak={"error": "org.plinius.Speech1.Error.NoVoice", "message": "no voice"})
    r = gcc_world.run(program, [listen("Kerstin"), settle(200), dump("main")])
    assert buttons(row(r.dump("main"), "Kerstin"))[0][1] == "Listen"
    (m,) = messages(r)
    assert m.startswith("Cannot let the voice de_DE-kerstin-low speak: ") and "no voice" in m


def test_a_refusal_after_a_stop_changes_nothing(program, gcc_world, voices):
    voices.speechd(Speak=[{"delay_ms": 300, "error": "org.plinius.Speech1.Error.NoVoice", "message": "late"},
                          "(uint32 21,)"])
    r = gcc_world.run(program, [listen("Thorsten"), listen("Kerstin"), wait(600), settle(), dump("main")])
    # The second click stopped the first sample and started Kerstin; the first's late refusal
    # leaves Kerstin playing
    assert buttons(row(r.dump("main"), "Kerstin"))[0][1] == "Stop"
    assert len(messages(r)) == 1


def test_the_service_leaving_ends_the_sample(program, gcc_world, voices):
    voices.speechd(Speak="(uint32 9,)")
    ctl = gcc_world.control("session")
    r = gcc_world.run(program, [listen("Thorsten"), settle(200), ctl.release(SPEECH), settle(200), dump("gone")])
    assert buttons(row(r.dump("gone"), "Thorsten"))[0][1] == "Listen"


def test_without_a_session_bus_nothing_plays(program, gcc_world, voices):
    r = gcc_world.run(program, [listen("Thorsten"), settle(), dump("main")], session_bus=False)
    assert buttons(row(r.dump("main"), "Thorsten"))[0][1] == "Listen"
    assert [m for m in messages(r) if m.startswith("No session bus: ")]


def test_choosing_while_it_plays_keeps_playing(program, gcc_world, voices):
    voices.speechd(Speak="(uint32 14,)")
    r = gcc_world.run(program, [listen("Kerstin"), settle(200), click(type="AdwActionRow", text="Kerstin"),
                                settle(), dump("main")])
    s = r.dump("main")
    assert chosen(s) == ["Kerstin"]
    assert buttons(row(s, "Kerstin"))[0][1] == "Stop"


# ----- Downloading and removing -----

@pytest.fixture
def german(phone):
    """Thorsten installed, Kerstin to download"""
    phone.index()
    phone.install(THORSTEN)
    return phone


def test_download_asks_with_the_size_then_installs_and_chooses(program, gcc_world, german):
    go = gcc_world.tmp / "go"
    german.pacman("Download Size   : 1.00 MiB")
    german.pkexec(voice=KERSTIN, wait_for=go)
    cache = german.path(CACHE)
    cache.mkdir(parents=True)
    big = 1024 * 1024
    pkg = "plinius-speech-voice-de-de-kerstin"
    r = gcc_world.run(program, [
        click(type="GtkButton", tooltip="Download"), wait_for(type="AdwAlertDialog"), dump("asked"),
        click(type="GtkButton", text="Download"), settle(), dump("preparing"),
        wait(700), dump("waiting"),
        {"op": "write_file", "path": str(cache / f"{pkg}-1-1-any.pkg.tar.zst.part"), "text": "x" * (big // 4)},
        # Signatures, other packages and a package whose name only starts the same do not count
        {"op": "write_file", "path": str(cache / f"{pkg}-1-1-any.pkg.tar.zst.sig"), "text": "x" * big},
        {"op": "write_file", "path": str(cache / f"{pkg}-1-1-any.pkg.tar.zst.sig.part"), "text": "x" * big},
        {"op": "write_file", "path": str(cache / f"{pkg}-high-1-1-any.pkg.tar.zst"), "text": "x" * big},
        {"op": "write_file", "path": str(cache / "plinius-speech-1-1-any.pkg.tar.zst"), "text": "x" * big},
        wait(700), dump("downloading"),
        {"op": "write_file", "path": str(cache / f"{pkg}-1-1-any.pkg.tar.zst"), "text": "x" * big},
        wait(700), dump("installing"),
        {"op": "write_file", "path": str(go), "text": ""},
        wait_for(type="AdwActionRow", text="Installing Voice…", gone=True, timeout=10000), settle(300),
        dump("done"), get("voices")])
    alert = r.dump("asked").one(type="AdwAlertDialog")
    assert alert["text"] == "Download Voice?"
    assert alert["subtitle"] == ("The voice Kerstin is downloaded now (1.0\xa0MB, 20.0\xa0MB once installed). It "
                                 "speaks without an internet connection.")
    assert german.bin.calls("pacman") == [["-Si", pkg]]
    pre = r.dump("preparing")
    assert pre.one(type="AdwActionRow", text="Preparing Download…")
    # While it runs, no voice can be chosen, heard or changed
    assert row(pre, "Thorsten").get("sensitive") is False
    assert row(pre, "Kerstin").get("sensitive") is False
    assert r.dump("waiting").has(type="AdwActionRow", text="Preparing Download…")
    down = r.dump("downloading").one(type="AdwActionRow", text="Downloading Voice…")
    assert down["subtitle"] == "262.1\xa0kB of 1.0\xa0MB"
    bar = [n for n in down.walk() if n["type"] == "GtkProgressBar"][0]
    assert bar["fraction"] == pytest.approx(0.25)
    assert not r.dump("installing").one(type="AdwActionRow", text="Installing Voice…").get("subtitle")
    done = r.dump("done")
    assert chosen(done) == ["Kerstin"]
    assert buttons(row(done, "Kerstin")) == [("media-playback-start-symbolic", "Listen")]
    assert row(done, "Thorsten").get("sensitive") is None
    assert not done.has(type="AdwActionRow", text="Installing Voice…")
    assert r.value("voices") == "{'de': 'de_DE-kerstin-low'}"
    assert german.pkexec_calls() == [[str(german.path(PKG_TOOL)), "install", pkg]]
    assert not r.warnings()


def test_the_row_of_a_voice_to_download_asks_too(program, gcc_world, german):
    german.pacman()
    r = gcc_world.run(program, [click(type="AdwActionRow", text="Kerstin"), wait_for(type="AdwAlertDialog"),
                                dump("asked")])
    assert r.dump("asked").one(type="AdwAlertDialog")["text"] == "Download Voice?"


def test_a_voice_of_another_region_says_it_in_the_dialogs(program, gcc_world, phone):
    phone.language("en_US.UTF-8")
    phone.index()
    phone.install(JOE)
    phone.install(KATHLEEN)
    phone.pacman()
    r = gcc_world.run(program, [click(type="GtkButton", tooltip="Download"), wait_for(type="AdwAlertDialog"),
                                dump("download"), click(type="GtkButton", text="Cancel"), settle(),
                                click(type="GtkButton", tooltip="Remove"), wait_for(type="AdwAlertDialog"),
                                dump("remove")])
    assert r.dump("download").one(type="AdwAlertDialog")["subtitle"].startswith(
        "The voice Cori (United Kingdom) is downloaded now (")
    assert r.dump("remove").one(type="AdwAlertDialog")["text"] == "Remove the Voice Kathleen (United States)?"


def test_a_voice_without_a_size_in_the_index_counts_the_download(program, gcc_world, phone):
    kerstin = {k: v for k, v in KERSTIN.items() if k != "size"}
    phone.index([THORSTEN, kerstin])
    phone.install(THORSTEN)
    phone.pacman("Download Size   : 2.00 MiB")
    r = gcc_world.run(program, [dump("main"), click(type="GtkButton", tooltip="Download"),
                                wait_for(type="AdwAlertDialog"), dump("asked")])
    assert row(r.dump("main"), "Kerstin")["subtitle"] == "Not installed"
    assert "(2.1\xa0MB, 2.1\xa0MB once installed)" in r.dump("asked").one(type="AdwAlertDialog")["subtitle"]


def test_cancel_downloads_nothing(program, gcc_world, german):
    german.pacman()
    german.pkexec(voice=KERSTIN)
    r = gcc_world.run(program, [click(type="GtkButton", tooltip="Download"), wait_for(type="AdwAlertDialog"),
                                click(type="GtkButton", text="Cancel"), settle(), dump("main"), get("voices")])
    assert german.pkexec_calls() == []
    assert not r.dump("main").has(type="AdwAlertDialog")
    assert r.value("voices") == "{}"


@pytest.mark.parametrize("line", ["Download Size   :", "Repository      : plinius"])
def test_a_voice_not_in_the_database_says_update_first(program, gcc_world, german, line):
    german.pacman(line)
    r = gcc_world.run(program, [click(type="GtkButton", tooltip="Download"), wait_for(type="AdwAlertDialog"),
                                dump("asked")])
    alert = r.dump("asked").one(type="AdwAlertDialog")
    assert (alert["text"], alert["subtitle"]) == ("Voice Not Available",
                                                  "Update the system in Software first, then try again.")


@pytest.mark.parametrize("line, shown", [
    ("Download Size   : 2.00 GiB", "2.1\xa0GB"),
    ("Download Size   : 512.00 KiB", "524.3\xa0kB"),
    ("Download Size   : 900 B", "900 bytes"),
])
def test_download_sizes(program, gcc_world, german, line, shown):
    german.pacman(line)
    r = gcc_world.run(program, [click(type="GtkButton", tooltip="Download"), wait_for(type="AdwAlertDialog"),
                                dump("asked")])
    assert f"({shown}, 20.0\xa0MB once installed)" in r.dump("asked").one(type="AdwAlertDialog")["subtitle"]


def test_without_pacman_nothing_is_asked(program, gcc_world, german):
    r = gcc_world.run(program, [click(type="GtkButton", tooltip="Download"), settle(200), dump("main")])
    assert not r.dump("main").has(type="AdwAlertDialog")


def test_installed_without_the_voice_afterwards_chooses_nothing(program, gcc_world, german):
    german.pacman()
    german.pkexec()
    r = gcc_world.run(program, [click(type="GtkButton", tooltip="Download"), wait_for(type="AdwAlertDialog"),
                                click(type="GtkButton", text="Download"), settle(500), dump("done"), get("voices")])
    assert r.value("voices") == "{}"
    assert chosen(r.dump("done")) == ["Thorsten"]
    assert not r.dump("done").has(type="AdwAlertDialog")


@pytest.mark.parametrize("code, message", [
    (3, "Update the system in Software first, then try again."),
    (4, "Update the system in Software first, then try again."),
    (9, "Another software installation is running. Try again when it is done."),
    (1, "Check the internet connection and try again."),
    (126, None),   # the password dialog closed
    (127, None),   # not authorized
])
def test_install_errors(program, gcc_world, german, code, message):
    german.pacman()
    german.pkexec(code=code, output="error: something")
    r = gcc_world.run(program, [click(type="GtkButton", tooltip="Download"), wait_for(type="AdwAlertDialog"),
                                click(type="GtkButton", text="Download"),
                                wait_for(type="AdwActionRow", text="Preparing Download…", gone=True),
                                settle(200), dump("done")])
    s = r.dump("done")
    alerts = s.find(type="AdwAlertDialog")
    if message is None:
        assert alerts == []
    else:
        assert [(a["text"], a["subtitle"]) for a in alerts] == [("Voice Not Installed", message)]
    assert row(s, "Kerstin").get("sensitive") is None and row(s, "Kerstin")["subtitle"].startswith("Not installed")
    assert messages(r) == [f"plinius-pkg ended with {code}: error: something"]


def test_without_pkexec_the_install_fails_at_once(program, gcc_world, german):
    german.pacman()
    r = gcc_world.run(program, [click(type="GtkButton", tooltip="Download"), wait_for(type="AdwAlertDialog"),
                                click(type="GtkButton", text="Download"), settle(200), dump("done")])
    alert = r.dump("done").one(type="AdwAlertDialog")
    assert (alert["text"], alert["subtitle"]) == ("Voice Not Installed",
                                                  "Check the internet connection and try again.")
    assert any(m.startswith("Cannot run ") for m in messages(r))
    assert not r.dump("done").has(type="AdwActionRow", text="Preparing Download…")


def test_a_download_stops_the_sample(program, gcc_world, german):
    german.speechd(Speak="(uint32 31,)")
    german.pacman()
    german.pkexec(voice=KERSTIN)
    gcc_world.run(program, [listen("Thorsten"), settle(200), click(type="GtkButton", tooltip="Download"),
                            wait_for(type="AdwAlertDialog"), click(type="GtkButton", text="Download"),
                            settle(500)])
    assert stops(gcc_world) == [[31]]


def test_remove_asks_then_removes(program, gcc_world, voices):
    go = gcc_world.tmp / "go"
    voices.pkexec(voice=KERSTIN, wait_for=go)
    r = gcc_world.run(program, [click(type="GtkButton", tooltip="Remove"), wait_for(type="AdwAlertDialog"),
                                dump("asked"), click(type="GtkButton", text="Remove"), settle(), wait(600),
                                dump("removing"),
                                {"op": "write_file", "path": str(go), "text": ""},
                                wait_for(type="AdwActionRow", text="Removing Voice…", gone=True, timeout=10000),
                                settle(200), dump("done")])
    alert = r.dump("asked").one(type="AdwAlertDialog")
    assert (alert["text"], alert["subtitle"]) == (
        "Remove the Voice Kerstin?", "This frees 20.0\xa0MB. The voice can be downloaded again later.")
    rem = r.dump("removing")
    assert rem.has(type="AdwActionRow", text="Removing Voice…")
    assert row(rem, "Kerstin").get("sensitive") is False
    done = r.dump("done")
    assert row(done, "Kerstin")["subtitle"] == "Not installed, 20.0\xa0MB"
    assert voices.pkexec_calls() == [[str(voices.path(PKG_TOOL)), "remove", "plinius-speech-voice-de-de-kerstin"]]
    assert not voices.voice_dir(KERSTIN["id"]).exists()
    assert not r.warnings()


def test_remove_without_a_size_says_less(program, gcc_world, phone):
    kerstin = {k: v for k, v in KERSTIN.items() if k != "size"}
    phone.index([THORSTEN, kerstin])
    phone.install(THORSTEN)
    phone.install(kerstin)
    r = gcc_world.run(program, [click(type="GtkButton", tooltip="Remove"), wait_for(type="AdwAlertDialog"),
                                dump("asked")])
    assert r.dump("asked").one(type="AdwAlertDialog")["subtitle"] == "The voice can be downloaded again later."


def test_remove_cancelled_keeps_the_voice(program, gcc_world, voices):
    voices.pkexec(voice=KERSTIN)
    r = gcc_world.run(program, [click(type="GtkButton", tooltip="Remove"), wait_for(type="AdwAlertDialog"),
                                click(type="GtkButton", text="Cancel"), settle(200), dump("done")])
    assert voices.pkexec_calls() == []
    assert ("user-trash-symbolic", "Remove") in buttons(row(r.dump("done"), "Kerstin"))


def test_remove_failing_says_so(program, gcc_world, voices):
    voices.pkexec(code=9)
    r = gcc_world.run(program, [click(type="GtkButton", tooltip="Remove"), wait_for(type="AdwAlertDialog"),
                                click(type="GtkButton", text="Remove"),
                                wait_for(type="AdwAlertDialog", text="Voice Not Removed"), dump("done")])
    alert = r.dump("done").one(type="AdwAlertDialog", text="Voice Not Removed")
    assert alert["subtitle"] == "Another software installation is running. Try again when it is done."
    assert ("user-trash-symbolic", "Remove") in buttons(row(r.dump("done"), "Kerstin"))
    # Without output nothing follows the code
    assert messages(r) == ["plinius-pkg ended with 9"]


def test_remove_without_pkexec(program, gcc_world, voices):
    r = gcc_world.run(program, [click(type="GtkButton", tooltip="Remove"), wait_for(type="AdwAlertDialog"),
                                click(type="GtkButton", text="Remove"),
                                wait_for(type="AdwAlertDialog", text="Voice Not Removed"), dump("done")])
    assert r.dump("done").one(type="AdwAlertDialog", text="Voice Not Removed")["subtitle"] == (
        "Check the internet connection and try again.")


# ----- Closing the page -----

def test_closing_while_everything_is_pending(program, gcc_world, german):
    """Speak() unanswered, pacman and plinius-pkg still running: the page goes without waiting"""
    german.speechd(Speak={"delay_ms": 3000, "reply": "(uint32 41,)"})
    german.bin.stub("pacman", "sleep 5")
    r = gcc_world.run(program, [listen("Thorsten"), click(type="GtkButton", tooltip="Download"), settle(100)])
    assert not r.warnings()


def test_closing_while_a_voice_installs(program, gcc_world, german):
    german.pacman()
    german.bin.stub("pkexec", "sleep 5")
    r = gcc_world.run(program, [click(type="GtkButton", tooltip="Download"), wait_for(type="AdwAlertDialog"),
                                click(type="GtkButton", text="Download"),
                                # No cache directory yet: the bar only pulses
                                wait(700), dump("waiting")])
    assert r.dump("waiting").has(type="AdwActionRow", text="Preparing Download…")
    assert not r.warnings()
