# Sounds and Vibration (cc-feedback-panel.c): a sound, a vibration rhythm and its strength for
# each kind of event, written into the feedback theme of the user's own
# ($XDG_CONFIG_HOME/feedbackd/themes/plinius.json) and made feedbackd's theme. feedbackd is a fake
# on the test's private session bus (org.sigxcpu.Feedback and .Haptic, which the tests' own
# interface describes as feedbackd 0.8 has it); the sound previews go to a GSound stand-in
# (c/gsound_stub.c) that records what would play; system sounds lie in the program's root.

import json
import shutil
import subprocess

import pytest

from gcc_harness import (CDIR, back, build_panel, call, click, dump, gcc_session,  # noqa: F401
                         gcc_world, select, set_active, set_text, settle, wait, wait_for)

FBD = "org.sigxcpu.Feedback"
FBD_PATH = "/org/sigxcpu/Feedback"
FBD_XML = f"""<node>
 <interface name="{FBD}">
  <method name="TriggerFeedback"><arg name="app_id" type="s" direction="in"/>
   <arg name="event" type="s" direction="in"/><arg name="hints" type="a{{sv}}" direction="in"/>
   <arg name="timeout" type="i" direction="in"/><arg name="id" type="u" direction="out"/></method>
  <method name="EndFeedback"><arg name="id" type="u" direction="in"/></method>
  <signal name="FeedbackEnded"><arg name="id" type="u"/><arg name="reason" type="u"/></signal>
  <property name="Profile" type="s" access="readwrite"/>
 </interface>
 <interface name="{FBD}.Haptic">
  <method name="Vibrate"><arg name="app_id" type="s" direction="in"/>
   <arg name="pattern" type="a(du)" direction="in"/><arg name="success" type="b" direction="out"/></method>
 </interface>
</node>"""

SCHEMA = """<schemalist>
  <schema id="org.sigxcpu.feedbackd" path="/org/sigxcpu/feedbackd/">
    <key name="profile" type="s">
      <choices><choice value='full'/><choice value='quiet'/><choice value='silent'/></choices>
      <default>'full'</default>
    </key>
    {theme}
  </schema>
</schemalist>
"""
THEME_KEY = "<key name='theme' type='s'><default>'default'</default></key>"

GLUE = r'''
#include "gcc_driver.h"
#include "cc-feedback-panel.h"

GType gcc_test_panel_get_type (void);

CcPanel *
gcc_test_create_panel (GccTest *t)
{
    /* Without feedbackd's schema the panel cannot be made: only the listing is asked */
    if (!g_strcmp0 (gcc_test_param (t, "panel"), "none"))
        return g_object_new (gcc_test_panel_get_type (), "window", t->window, NULL);
    return g_object_new (CC_TYPE_FEEDBACK_PANEL, "window", t->window, NULL);
}

void
gcc_test_setup (GccTest *t)
{
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "static_init")) {
        cc_feedback_panel_static_init_func ();
        json_builder_add_null_value (result);
        return TRUE;
    }
    return FALSE;
}
'''

CHANNELS = ["Calls", "Messages", "Email", "Other Notifications", "Alarms", "Keyboard", "Alert Sound"]
STEREO = ["phone-incoming-call", "phone-outgoing-calling", "alarm-clock-elapsed", "message-new-instant",
          "message", "bell", "complete", "dialog-information", "audio-volume-change", "dialog-warning",
          "window-attention", "service-login"]
ALERTS = ["click", "hum", "string", "swing"]
OGG = b"OggS" + bytes(24) + b"\x01vorbis" + bytes(29)
WAV = b"RIFF" + bytes(4) + b"WAVEfmt " + bytes(52)


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    out = tmp_path_factory.mktemp("feedback")
    root = out / "feedback-root"
    return build_panel(gcc_session, out, "feedback", ["cc-feedback-panel.c"], GLUE,
                       flags=[f'-DSOUND_DATA_DIR="{root}/sounds"', str(CDIR / "gsound_stub.c")])


def _schemas(session, base, theme=True, feedbackd=True):
    d = base / ("schemas-" + ("t" if theme else "n") + ("f" if feedbackd else "x"))
    if d.exists():
        return d
    d.mkdir()
    for f in session.schemas.glob("*.gschema.xml"):
        shutil.copy(f, d / f.name)
    if feedbackd:
        (d / "org.sigxcpu.feedbackd.gschema.xml").write_text(SCHEMA.format(theme=THEME_KEY if theme else ""))
    subprocess.run(["glib-compile-schemas", str(d)], check=True, capture_output=True)
    return d


@pytest.fixture(scope="module")
def schema_dirs(gcc_session, tmp_path_factory):
    base = tmp_path_factory.mktemp("feedback-schemas")
    return {"full": _schemas(gcc_session, base), "no-theme": _schemas(gcc_session, base, theme=False),
            "none": _schemas(gcc_session, base, feedbackd=False)}


class World:
    """One test's files: the system sounds in the program's root, the theme file, own sounds,
    the GSound log."""

    def __init__(self, program, world, schema_dirs):
        self.world = world
        self.sounds = program.root / "sounds"
        shutil.rmtree(self.sounds, ignore_errors=True)
        for name in STEREO:
            self.system(f"freedesktop/stereo/{name}.oga")
        for name in ALERTS:
            self.system(f"gnome/default/alerts/{name}.ogg")
        self.config = world.home / ".config"
        self.theme_file = self.config / "feedbackd/themes/plinius.json"
        self.own = world.home / ".local/share/plinius/sounds"
        self.log = world.tmp / "gsound.jsonl"
        world.extra_env.update({"GSETTINGS_SCHEMA_DIR": str(schema_dirs["full"]),
                                "PLINIUS_TEST_GSOUND_LOG": str(self.log)})

    def system(self, rel, data=OGG):
        f = self.sounds / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
        return str(f)

    def stereo(self, name):
        return str(self.sounds / "freedesktop/stereo" / f"{name}.oga")

    def alert(self, name):
        return str(self.sounds / "gnome/default/alerts" / f"{name}.ogg")

    def write_theme(self, data):
        self.theme_file.parent.mkdir(parents=True, exist_ok=True)
        self.theme_file.write_text(json.dumps(data) if not isinstance(data, str) else data)

    def theme(self):
        return json.loads(self.theme_file.read_text())

    def own_sound(self, name, data=OGG):
        self.own.mkdir(parents=True, exist_ok=True)
        f = self.own / name
        f.write_bytes(data)
        return str(f)

    def music(self, folder):
        """XDG_MUSIC_DIR (user-dirs.dirs)"""
        folder.mkdir(parents=True, exist_ok=True)
        (self.config / "user-dirs.dirs").write_text(f'XDG_MUSIC_DIR="{folder}"\n')

    def plays(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def feedbackd(self, **replies):
        self.world.service("session", FBD, FBD_PATH, FBD_XML, replies=replies,
                           properties={"Profile": "'full'"})


@pytest.fixture
def fb(program, gcc_world, schema_dirs):
    return World(program, gcc_world, schema_dirs)


def theme_set(value):
    return {"op": "gsettings_set", "schema": "org.sigxcpu.feedbackd", "key": "theme", "value": repr(value)}


def theme_get(label="theme"):
    return {"op": "gsettings_get", "schema": "org.sigxcpu.feedbackd", "key": "theme", "label": label}


SETUP = {"op": "setup"}

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_XML = """<node><interface name="org.freedesktop.portal.FileChooser">
 <method name="OpenFile"><arg type="s" name="parent_window" direction="in"/>
  <arg type="s" name="title" direction="in"/><arg type="a{sv}" name="options" direction="in"/>
  <arg type="o" name="handle" direction="out"/></method>
 <property name="version" type="u" access="read"/>
</interface></node>"""


def summaries(screen):
    return {r["text"]: r.get("subtitle") for r in screen.find(type="AdwActionRow") if r.get("text") in CHANNELS}


def test_first_page_lists_every_kind_with_its_defaults(program, gcc_world, fb):
    fb.feedbackd()
    r = gcc_world.run(program, [settle(), dump("main"), theme_get()])
    s = r.dump("main")
    assert summaries(s) == {
        "Calls": "Ringing · Default Vibration",
        "Messages": "New Message · Default Vibration",
        "Email": "Message · Default Vibration",
        "Other Notifications": "No Sound · Default Vibration",
        "Alarms": "Alarm Clock · Default Vibration",
        "Keyboard": "No Sound · Default Vibration",
        "Alert Sound": "No Sound · No Vibration",
    }
    assert s.has(type="CcFeedbackPanel", text="Sounds and Vibration")
    # Looking writes nothing
    assert r.value("theme") == "'default'"
    assert not fb.theme_file.exists()
    assert not r.warnings()



# ----- Helpers -----

def open_channel(title):
    return [click(type="AdwActionRow", text=title), settle()]


def open_sounds(title):
    return [*open_channel(title), click(type="AdwActionRow", text="Sound"), settle()]


def choose(title):
    return set_active(True, type="GtkCheckButton", within={"type": "AdwActionRow", "text": title})


def rhythm(item):
    return select(item=item, type="AdwComboRow", text="Rhythm")


def strength(value):
    return {"op": "set_value", "target": {"type": "GtkScale"}, "value": value}


def sound(event, file, role=None):
    entry = {"event-name": event, "type": "Sound", "file-name": file}
    if role:
        entry["media-role"] = role
    return entry


def dummy(event):
    return {"event-name": event, "type": "Dummy", "duration": 1000}


def vibra(event, magnitudes, durations):
    return {"event-name": event, "type": "VibraPattern", "magnitudes": magnitudes, "durations": durations}


def theme(full=(), quiet=(), silent=(), parent="default"):
    return {"name": "plinius", "parent-name": parent,
            "profiles": [{"name": "full", "feedbacks": list(full)},
                         {"name": "quiet", "feedbacks": list(quiet)},
                         {"name": "silent", "feedbacks": list(silent)}]}


def channel_state(screen):
    """Sound subtitle, rhythm, strength label, whether strength and reset can be used."""
    combo = screen.one(type="AdwComboRow", text="Rhythm")
    row = screen.one(type="AdwActionRow", text="Strength")
    label = [c["text"] for c in row.get("children", []) if c["type"] == "GtkLabel"]
    return {"sound": screen.one(type="AdwActionRow", text="Sound").get("subtitle"),
            "rhythm": combo["selected_text"], "strength": label[0] if label else None,
            "strength_sensitive": row.get("sensitive", True),
            "reset": screen.has(type="AdwButtonRow", text="Reset to Default")}


def checked(screen):
    """The title of the sound row whose check is on."""
    rows = [r for r in screen.find(type="AdwActionRow")
            if any(c["type"] == "GtkCheckButton" and c.get("active") for c in r.get("children", []))]
    return [r["text"] for r in rows]


def group_rows(screen, title):
    return [r["text"] for r in screen.within(type="AdwPreferencesGroup", text=title).find(type="AdwActionRow")]


def messages(r):
    return [m["message"] for m in r.warnings()]


# ----- The listing -----

@pytest.mark.parametrize("schemas, panel, shown", [("full", None, "visible"), ("no-theme", "none", "hidden"),
                                                   ("none", "none", "hidden")])
def test_listed_only_with_feedbackds_theme_setting(program, gcc_world, fb, schema_dirs, schemas, panel, shown):
    gcc_world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(schema_dirs[schemas])
    r = gcc_world.run(program, [call("static_init")], params={"panel": panel} if panel else {})
    assert r.visibility == {"feedback": shown}


# ----- Reading the theme -----

def test_choices_are_read_back_from_the_theme_file(program, gcc_world, fb):
    fb.feedbackd()
    fb.write_theme(theme(
        full=[sound("phone-incoming-call", fb.stereo("bell")), dummy("message-new-instant"),
              {"event-name": "message-new-email", "type": "Sound"},
              sound("key-pressed", "/nowhere/My Click.wav"), sound("bell-window-system", "/nowhere/.wav")],
        quiet=[vibra("phone-incoming-call", [0.4], [1000]),
               {"event-name": "message-new-instant", "type": "VibraPattern", "magnitudes": [1]},
               vibra("message-new-email", [], []),
               vibra("notification-new-generic", [0.6, 0, 0.6], [150, 120, 150]),
               dummy("alarm-clock-elapsed"), vibra("key-pressed", [0.05], [35]),
               vibra("bell-window-system", [0, 0], [150, 850])],
        parent="custom"))
    r = gcc_world.run(program, [theme_set("plinius"), SETUP, settle(), dump("main")])
    assert summaries(r.dump("main")) == {
        "Calls": "Bell · Continuous, 40 %",
        "Messages": "No Sound · Default Vibration",
        "Email": "Message · Default Vibration",
        "Other Notifications": "No Sound · Double, 60 %",
        "Alarms": "Alarm Clock · No Vibration",
        # Weaker than the weakest strength offered: the weakest
        "Keyboard": "My Click · Tap, 10 %",
        # A rhythm without any strength is none of ours
        "Alert Sound": ".wav · No Vibration",
    }


def test_odd_entries_of_a_hand_written_theme_are_skipped(program, gcc_world, fb):
    fb.feedbackd()
    fb.write_theme({"name": "plinius", "parent-name": 7, "profiles": [
        3, {"name": "full", "feedbacks": [1, {"event-name": 5, "type": "Dummy"},
                                          {"event-name": "phone-incoming-call", "type": "Ringtone"}]},
        {"name": "silent", "feedbacks": "none"},
        {"name": "quiet", "feedbacks": [
            vibra("phone-incoming-call", ["1"], [1000]), vibra("message-new-instant", [1, 0], [150]),
            vibra("message-new-email", [[1]], [{"ms": 150}]), vibra("alarm-clock-elapsed", [True], [1000])]},
        {"name": "quiet", "feedbacks": [vibra("notification-new-generic", [1, 0, 1, 0], [150, 120, 150, 900])]}]})
    r = gcc_world.run(program, [theme_set("plinius"), SETUP, settle(), dump("main")])
    s = summaries(r.dump("main"))
    assert s["Calls"] == "Ringing · Default Vibration"
    assert s["Messages"] == "New Message · Default Vibration"
    assert s["Email"] == "Message · Default Vibration"
    # The first quiet profile counts, the second is not read
    assert s["Other Notifications"] == "No Sound · Default Vibration"
    assert s["Alarms"] == "Alarm Clock · Default Vibration"
    assert not r.warnings()


@pytest.mark.parametrize("current, file, parent", [
    ("plinius", {"parent-name": "custom"}, "custom"),
    ("plinius", {"parent-name": "plinius"}, "default"),
    ("plinius", None, "default"),
    ("plinius", "[1, 2]", "default"),
    ("custom", {"parent-name": "other"}, "custom"),
    ("", None, "default"),
])
def test_the_theme_before_is_the_parent(program, gcc_world, fb, current, file, parent):
    fb.feedbackd()
    if isinstance(file, dict):
        fb.write_theme({"name": "plinius", **file, "profiles": [
            {"name": "full", "feedbacks": [dummy("message-new-email")]}]})
    elif file:
        fb.write_theme(file)
    r = gcc_world.run(program, [theme_set(current), SETUP, settle(), dump("main"),
                                *open_sounds("Calls"), choose("None"), settle(), theme_get()])
    written = fb.theme()
    assert written["parent-name"] == parent
    assert r.value("theme") == "'plinius'"
    # The file's entries are only taken while it is the theme
    email = summaries(r.dump("main"))["Email"]
    assert email == ("No Sound · Default Vibration" if current == "plinius" and isinstance(file, dict)
                     else "Message · Default Vibration")


def test_no_sound_previews_without_a_sound_context(program, gcc_world, fb):
    fb.feedbackd()
    gcc_world.extra_env["PLINIUS_TEST_GSOUND_NEW_FAIL"] = "no PulseAudio"
    r = gcc_world.run(program, [*open_sounds("Calls"), choose("Bell"), settle()])
    assert messages(r) == ["No sound previews: no PulseAudio"]
    assert fb.plays() == []
    # The choice is kept all the same
    assert fb.theme() == theme(full=[sound("phone-incoming-call", fb.stereo("bell"), "Ringtone")])


# ----- Sounds -----

def test_sound_page_offers_default_none_and_the_system_sounds(program, gcc_world, fb):
    fb.feedbackd()
    (fb.sounds / "freedesktop/stereo/service-login.oga").unlink()
    r = gcc_world.run(program, [*open_sounds("Calls"), dump("sounds")])
    s = r.dump("sounds")
    assert s.one(type="AdwActionRow", text="Default")["subtitle"] == "Ringing"
    assert checked(s) == ["Default"]
    assert group_rows(s, "System Sounds") == [
        "Ringing", "Calling", "Alarm Clock", "New Message", "Message", "Bell", "Complete", "Information",
        "Tick", "Warning", "Attention", "Click", "Hum", "String", "Swing"]
    assert group_rows(s, "Own Sounds") == []
    assert s.one(type="AdwPreferencesGroup", text="Own Sounds")["subtitle"] == (
        "Ogg Vorbis or WAV. The file is copied, so it stays when the original is deleted.")
    assert s.has(type="GtkButton", text="Add", tooltip="Add a Sound of Your Own")


def test_the_keyboard_offers_only_short_sounds(program, gcc_world, fb):
    fb.feedbackd()
    r = gcc_world.run(program, [*open_sounds("Keyboard"), dump("sounds")])
    s = r.dump("sounds")
    assert s.one(type="AdwActionRow", text="Default")["subtitle"] == "No Sound"
    assert group_rows(s, "System Sounds") == ["Bell", "Information", "Tick", "Click"]


def test_a_system_sound_is_written_and_previewed(program, gcc_world, fb):
    fb.feedbackd()
    r = gcc_world.run(program, [*open_sounds("Calls"), choose("Bell"), settle(), dump("sounds"), back(),
                                settle(), dump("channel"), back(), settle(), dump("main"), theme_get()])
    assert fb.theme() == theme(full=[sound("phone-incoming-call", fb.stereo("bell"), "Ringtone")])
    assert r.value("theme") == "'plinius'"
    assert checked(r.dump("sounds")) == ["Bell"]
    assert channel_state(r.dump("channel"))["sound"] == "Bell"
    assert channel_state(r.dump("channel"))["reset"]
    assert summaries(r.dump("main"))["Calls"] == "Bell · Default Vibration"
    assert fb.plays()[0] == {"op": "play", "id": 1, "attrs": {
        "media.filename": fb.stereo("bell"), "media.role": "Ringtone", "application.id": "org.gnome.Settings"}}
    # The theme was not plinius yet: feedbackd follows the setting, no signal needed
    assert not gcc_world.calls("signal")
    assert not r.warnings()


def test_none_is_written_for_both_message_events(program, gcc_world, fb):
    fb.feedbackd()
    gcc_world.run(program, [*open_sounds("Messages"), choose("None"), settle()])
    assert fb.theme() == theme(full=[dummy("message-new-instant"), dummy("message-new-sms")])
    # Nothing to hear
    assert fb.plays() == []


@pytest.mark.parametrize("channel, attrs", [
    ("Calls", {"event.id": "phone-incoming-call", "media.role": "Ringtone"}),
    ("Email", {"event.id": "message-new-email", "media.role": "event"}),
    ("Other Notifications", None),
])
def test_the_default_sound_plays_from_the_sound_theme(program, gcc_world, fb, channel, attrs):
    fb.feedbackd()
    gcc_world.run(program, [{"op": "gsettings_set", "schema": "org.gnome.desktop.sound", "key": "theme-name",
                             "value": "'plinius-sounds'"},
                            *open_sounds(channel), choose("Hum"), settle(), choose("Default"), settle()])
    plays = [p for p in fb.plays() if p["op"] == "play"]
    if attrs is None:
        assert [p["attrs"].get("media.filename") for p in plays] == [fb.alert("hum")]
    else:
        assert plays[-1]["attrs"] == {**attrs, "canberra.xdg-theme.name": "plinius-sounds",
                                      "application.id": "org.gnome.Settings"}
    # Back to the default: nothing left in the theme, which goes back to the one before
    assert not fb.theme_file.exists()


def stop_button(screen, title):
    row = screen.one(type="AdwActionRow", text=title)
    return [c for c in row.get("children", []) if c.get("tooltip") == "Stop"]


def test_stop_button_shows_while_the_sound_plays(program, gcc_world, fb):
    fb.feedbackd()
    gcc_world.extra_env["PLINIUS_TEST_GSOUND_MS"] = "100000"
    stop = click(type="GtkButton", tooltip="Stop", within={"type": "AdwActionRow", "text": "Bell"})
    r = gcc_world.run(program, [*open_sounds("Calls"), choose("Bell"), settle(), dump("playing"),
                                # The row of the sound chosen: the sound again, the button stays
                                click(type="AdwActionRow", text="Bell"), settle(), dump("again"),
                                stop, settle(), dump("stopped")])
    assert stop_button(r.dump("playing"), "Bell")
    assert stop_button(r.dump("again"), "Bell")
    assert not stop_button(r.dump("stopped"), "Bell")
    # Each play ends cancelled: the first by the second, the second by the button
    assert sorted((p["id"], p["op"], p.get("how")) for p in fb.plays()) == [
        (1, "end", "cancelled"), (1, "play", None), (2, "end", "cancelled"), (2, "play", None)]
    assert not r.warnings()


def test_another_sound_ends_the_one_playing(program, gcc_world, fb):
    fb.feedbackd()
    gcc_world.extra_env["PLINIUS_TEST_GSOUND_MS"] = "100000"
    r = gcc_world.run(program, [*open_sounds("Calls"), choose("Bell"), settle(),
                                # A row not chosen yet: chosen
                                click(type="AdwActionRow", text="Click"), settle(), dump("other")])
    assert not stop_button(r.dump("other"), "Bell")
    assert stop_button(r.dump("other"), "Click")
    assert checked(r.dump("other")) == ["Click"]
    assert [p["attrs"]["media.filename"] for p in fb.plays() if p["op"] == "play"] == [
        fb.stereo("bell"), fb.alert("click")]


def test_leaving_the_page_ends_the_sound(program, gcc_world, fb):
    fb.feedbackd()
    gcc_world.extra_env["PLINIUS_TEST_GSOUND_MS"] = "100000"
    gcc_world.run(program, [*open_sounds("Calls"), choose("Bell"), settle(), back(), settle()])
    assert [(p["op"], p.get("how")) for p in fb.plays()] == [("play", None), ("end", "cancelled")]


def test_sound_that_ended_takes_its_button(program, gcc_world, fb):
    fb.feedbackd()
    gcc_world.extra_env["PLINIUS_TEST_GSOUND_MS"] = "50"
    r = gcc_world.run(program, [*open_sounds("Calls"), choose("Bell"), wait(300), settle(), dump("ended")])
    assert not stop_button(r.dump("ended"), "Bell")
    assert fb.plays()[-1]["how"] == "done"
    assert not r.warnings()


def test_a_sound_that_cannot_play_says_so(program, gcc_world, fb):
    fb.feedbackd()
    gcc_world.extra_env.update({"PLINIUS_TEST_GSOUND_MS": "20", "PLINIUS_TEST_GSOUND_FAIL": "No such device"})
    r = gcc_world.run(program, [*open_sounds("Calls"), choose("Bell"), wait(200), settle(), dump("failed")])
    assert messages(r) == ["Could not play the sound: No such device"]
    assert not stop_button(r.dump("failed"), "Bell")


# ----- Vibration -----

def vibrates(world):
    return [c["args"] for c in world.calls(FBD, "Vibrate")]


def test_channel_page_shows_the_setting(program, gcc_world, fb):
    fb.feedbackd()
    r = gcc_world.run(program, [*open_channel("Calls"), dump("calls"), back(), settle(),
                                *open_channel("Keyboard"), dump("keys")])
    assert channel_state(r.dump("calls")) == {"sound": "Ringing", "rhythm": "Default", "strength": "50 %",
                                              "strength_sensitive": True, "reset": False}
    assert r.dump("calls").one(type="AdwComboRow", text="Rhythm")["items"] == [
        "Default", "Off", "Short", "Long", "Double", "Triple", "Heartbeat", "Rising", "Continuous"]
    assert r.dump("keys").one(type="AdwComboRow", text="Rhythm")["items"] == [
        "Default", "Off", "Short Tap", "Tap", "Long Tap"]
    # Only what rings says how long the test rings
    assert r.dump("calls").has(type="AdwPreferencesGroup", subtitle="Rings for a few seconds, as the phone will.")
    assert not r.dump("keys").has(type="AdwPreferencesGroup", subtitle="Rings for a few seconds, as the phone will.")


def test_a_rhythm_is_written_and_felt_once(program, gcc_world, fb):
    fb.feedbackd()
    r = gcc_world.run(program, [*open_channel("Other Notifications"), rhythm("Triple"), settle(), dump("triple")])
    # A single event: without the pause at the end; the strength the default comes closest to
    assert fb.theme() == theme(quiet=[vibra("notification-new-generic", [0.25, 0, 0.25, 0, 0.25],
                                            [120, 100, 120, 100, 120])])
    assert vibrates(gcc_world) == [["org.gnome.Settings", [[0.25, 120], [0, 100], [0.25, 120], [0, 100],
                                                           [0.25, 120]]]]
    assert channel_state(r.dump("triple")) == {"sound": "No Sound", "rhythm": "Triple", "strength": "25 %",
                                               "strength_sensitive": True, "reset": True}


def test_a_ring_keeps_its_pause_but_is_felt_once(program, gcc_world, fb):
    fb.feedbackd()
    gcc_world.run(program, [*open_channel("Calls"), rhythm("Short"), settle()])
    assert fb.theme() == theme(quiet=[vibra("phone-incoming-call", [0.5, 0], [150, 850])])
    assert vibrates(gcc_world) == [["org.gnome.Settings", [[0.5, 150]]]]


def test_off_and_back_to_default(program, gcc_world, fb):
    fb.feedbackd()
    r = gcc_world.run(program, [*open_channel("Messages"), rhythm("Off"), settle(), dump("off")])
    assert fb.theme() == theme(quiet=[dummy("message-new-instant"), dummy("message-new-sms")])
    off = channel_state(r.dump("off"))
    assert off["rhythm"] == "Off" and off["strength_sensitive"] is False and off["reset"]
    # Nothing to feel
    assert vibrates(gcc_world) == []


def test_default_again_resets_the_strength_and_removes_the_theme(program, gcc_world, fb):
    fb.feedbackd()
    r = gcc_world.run(program, [*open_channel("Email"), rhythm("Long"), strength(80), wait(500), settle(),
                                rhythm("Default"), settle(), dump("default"), theme_get()])
    assert channel_state(r.dump("default"))["strength"] == "100 %"
    assert not fb.theme_file.exists()
    assert r.value("theme") == "'default'"


def test_strength_of_the_default_becomes_the_closest_rhythm(program, gcc_world, fb):
    fb.feedbackd()
    r = gcc_world.run(program, [*open_channel("Calls"), strength(70), settle(), dump("moved"), wait(500),
                                settle()])
    assert channel_state(r.dump("moved"))["rhythm"] == "Continuous"
    # Saved and felt once the slider rests
    assert fb.theme() == theme(quiet=[vibra("phone-incoming-call", [0.7], [1000])])
    assert vibrates(gcc_world) == [["org.gnome.Settings", [[0.7, 1000]]]]


def test_strength_still_moving_is_kept_when_leaving(program, gcc_world, fb):
    fb.feedbackd()
    gcc_world.run(program, [*open_channel("Messages"), strength(40), back(), settle(), wait(500)])
    assert fb.theme() == theme(quiet=[vibra("message-new-instant", [0.4], [150]),
                                      vibra("message-new-sms", [0.4], [150])])
    assert vibrates(gcc_world) == []


def test_reset_brings_back_the_default(program, gcc_world, fb):
    fb.feedbackd()
    fb.write_theme(theme(full=[sound("alarm-clock-elapsed", fb.stereo("bell"))],
                         quiet=[vibra("alarm-clock-elapsed", [1, 0, 1, 0], [150, 120, 150, 900])]))
    r = gcc_world.run(program, [theme_set("plinius"), SETUP, *open_channel("Alarms"), dump("before"),
                                click(type="AdwButtonRow", text="Reset to Default"), settle(), dump("after"),
                                back(), settle(), dump("main"), theme_get()])
    assert channel_state(r.dump("before"))["rhythm"] == "Double"
    assert channel_state(r.dump("after")) == {"sound": "Alarm Clock", "rhythm": "Default", "strength": "50 %",
                                              "strength_sensitive": True, "reset": False}
    assert summaries(r.dump("main"))["Alarms"] == "Alarm Clock · Default Vibration"
    assert not fb.theme_file.exists()
    assert r.value("theme") == "'default'"


def test_entries_of_other_events_are_kept(program, gcc_world, fb):
    fb.feedbackd()
    other = {"event-name": "battery-caution", "type": "VibraRumble", "duration": 500}
    fb.write_theme(theme(full=[other, sound("phone-incoming-call", fb.stereo("bell"))],
                         silent=[{"event-name": "battery-caution", "type": "Led"}, "x"]))
    r = gcc_world.run(program, [theme_set("plinius"), SETUP, *open_sounds("Calls"), choose("Default"),
                                settle(), theme_get()])
    assert fb.theme() == theme(full=[other], silent=[{"event-name": "battery-caution", "type": "Led"}])
    assert r.value("theme") == "'plinius'"
    # The file changed under the same name: feedbackd is told to read it again
    assert gcc_world.calls("signal", "SIGHUP")


def test_a_changed_theme_without_feedbackd_is_read_when_it_starts(program, gcc_world, fb):
    fb.write_theme(theme(full=[sound("phone-incoming-call", fb.stereo("bell"))]))
    r = gcc_world.run(program, [theme_set("plinius"), SETUP, *open_sounds("Calls"), choose("Hum"), settle()])
    assert fb.theme() == theme(full=[sound("phone-incoming-call", fb.alert("hum"), "Ringtone")])
    assert not gcc_world.calls("signal")
    assert not r.warnings()


def test_without_session_bus_choices_are_still_written(program, gcc_world, fb):
    fb.feedbackd()
    fb.write_theme(theme(full=[sound("phone-incoming-call", fb.stereo("bell"))]))
    r = gcc_world.run(program, [theme_set("plinius"), SETUP, *open_channel("Calls"), rhythm("Long"),
                                settle(), click(type="AdwButtonRow", text="Test"), settle(), dump("calls")],
                      session_bus=False)
    assert fb.theme() == theme(full=[sound("phone-incoming-call", fb.stereo("bell"), "Ringtone")],
                               quiet=[vibra("phone-incoming-call", [0.5, 0], [600, 900])])
    # Nobody to ask for a test
    assert r.dump("calls").has(type="AdwButtonRow", text="Test")
    assert not gcc_world.calls()


# ----- Saving fails -----

def test_theme_that_cannot_be_written_or_removed(program, gcc_world, fb):
    fb.feedbackd()
    fb.theme_file.mkdir(parents=True)
    r = gcc_world.run(program, [theme_set("plinius"), SETUP, *open_sounds("Calls"), choose("None"), settle(),
                                choose("Default"), settle(), theme_get()])
    warnings = messages(r)
    assert len(warnings) == 2
    assert warnings[0].startswith("Could not write the feedback theme: ")
    assert warnings[1] == f"Could not remove {fb.theme_file}: Is a directory"
    assert r.value("theme") == "'default'"


def test_theme_folder_that_cannot_be_made(program, gcc_world, fb):
    fb.feedbackd()
    fb.config.mkdir(parents=True, exist_ok=True)
    (fb.config / "feedbackd").write_text("")
    r = gcc_world.run(program, [*open_sounds("Calls"), choose("None"), settle(), theme_get()])
    assert messages(r) == [f"Could not create {fb.theme_file.parent}: Not a directory"]
    assert r.value("theme") == "'default'"


# ----- Test -----

def test_test_rings_and_ends_when_feedbackd_says_so(program, gcc_world, fb):
    fb.feedbackd(TriggerFeedback="(uint32 7,)")
    ctl = gcc_world.control("session")
    r = gcc_world.run(program, [*open_channel("Calls"), click(type="AdwButtonRow", text="Test"), settle(),
                                dump("testing"),
                                ctl.emit(FBD_PATH, FBD, "FeedbackEnded", "(uint32 6, uint32 0)"), settle(),
                                dump("other"),
                                ctl.emit(FBD_PATH, FBD, "FeedbackEnded", "(uint32 7, uint32 0)"), settle(),
                                dump("ended"),
                                # Again: the same subscription
                                click(type="AdwButtonRow", text="Test"), settle(),
                                ctl.emit(FBD_PATH, FBD, "FeedbackEnded", "(uint32 7, uint32 1)"), settle(),
                                dump("again")])
    assert [c["args"] for c in gcc_world.calls(FBD, "TriggerFeedback")] == [
        ["org.gnome.Settings", "phone-incoming-call", {}, 8]] * 2
    assert r.dump("testing").one(type="AdwButtonRow", text="Stop")["children"][0]["icon"] == (
        "media-playback-stop-symbolic")
    assert r.dump("other").has(type="AdwButtonRow", text="Stop")
    assert r.dump("ended").has(type="AdwButtonRow", text="Test")
    assert r.dump("again").has(type="AdwButtonRow", text="Test")
    assert not gcc_world.calls(FBD, "EndFeedback")


def test_stop_ends_the_test(program, gcc_world, fb):
    fb.feedbackd(TriggerFeedback="(uint32 9,)")
    r = gcc_world.run(program, [*open_channel("Messages"), click(type="AdwButtonRow", text="Test"), settle(),
                                click(type="AdwButtonRow", text="Stop"), settle(), dump("stopped")])
    # A single event plays once
    assert [c["args"] for c in gcc_world.calls(FBD, "TriggerFeedback")] == [
        ["org.gnome.Settings", "message-new-instant", {}, -1]]
    assert [c["args"] for c in gcc_world.calls(FBD, "EndFeedback")] == [[9]]
    assert r.dump("stopped").has(type="AdwButtonRow", text="Test")


def test_stop_before_feedbackd_answered(program, gcc_world, fb):
    fb.feedbackd(TriggerFeedback={"delay_ms": 400, "reply": "(uint32 11,)"})
    r = gcc_world.run(program, [*open_channel("Alarms"), click(type="AdwButtonRow", text="Test"),
                                click(type="AdwButtonRow", text="Stop"), settle(10), dump("waiting"),
                                wait(700), settle(), dump("stopped")])
    assert r.dump("waiting").has(type="AdwButtonRow", text="Stop")
    assert [c["args"] for c in gcc_world.calls(FBD, "EndFeedback")] == [[11]]
    assert r.dump("stopped").has(type="AdwButtonRow", text="Test")


def test_answer_after_the_test_ended(program, gcc_world, fb):
    fb.feedbackd(TriggerFeedback={"delay_ms": 400, "reply": "(uint32 12,)"})
    ctl = gcc_world.control("session")
    r = gcc_world.run(program, [*open_channel("Email"), click(type="AdwButtonRow", text="Test"),
                                # feedbackd says event 0 ended before its answer: the test it was
                                ctl.emit(FBD_PATH, FBD, "FeedbackEnded", "(uint32 0, uint32 0)"), settle(10),
                                wait(700), settle(), dump("ended")])
    assert r.dump("ended").has(type="AdwButtonRow", text="Test")
    assert not gcc_world.calls(FBD, "EndFeedback")


@pytest.mark.parametrize("service", [True, False])
def test_test_without_feedbackd_ends_at_once(program, gcc_world, fb, service):
    if service:
        fb.feedbackd(TriggerFeedback={"error": "org.freedesktop.DBus.Error.Failed", "message": "no"})
    r = gcc_world.run(program, [*open_channel("Calls"), click(type="AdwButtonRow", text="Test"), settle(),
                                dump("calls")])
    assert r.dump("calls").has(type="AdwButtonRow", text="Test")


def test_leaving_the_page_ends_the_test(program, gcc_world, fb):
    fb.feedbackd(TriggerFeedback="(uint32 5,)")
    gcc_world.run(program, [*open_channel("Calls"), click(type="AdwButtonRow", text="Test"), settle(), back(),
                            settle()])
    assert [c["args"] for c in gcc_world.calls(FBD, "EndFeedback")] == [[5]]


def test_test_ends_by_itself_when_feedbackd_never_says(program, gcc_world, fb):
    fb.feedbackd(TriggerFeedback="(uint32 3,)")
    r = gcc_world.run(program, [*open_channel("Calls"), click(type="AdwButtonRow", text="Test"), settle(),
                                wait(10500), dump("still"), wait(1000), settle(), dump("over")])
    # Eight seconds of ringing and three to spare
    assert r.dump("still").has(type="AdwButtonRow", text="Stop")
    assert r.dump("over").has(type="AdwButtonRow", text="Test")
    assert not gcc_world.calls(FBD, "EndFeedback")


def test_closing_settings_ends_the_test(program, gcc_world, fb):
    fb.feedbackd(TriggerFeedback="(uint32 4,)")
    gcc_world.run(program, [*open_channel("Calls"), click(type="AdwButtonRow", text="Test"), settle()])
    assert [c["args"] for c in gcc_world.calls(FBD, "EndFeedback")] == [[4]]


# ----- Own sounds -----

def edit(title):
    return click(type="GtkButton", tooltip="Rename or Delete", within={"type": "AdwActionRow", "text": title})


def respond(label):
    return click(type="GtkButton", text=label, within={"type": "AdwAlertDialog"})


def alert(screen):
    d = screen.find(type="AdwAlertDialog")
    return (d[0]["text"], d[0].get("subtitle")) if d else None


def test_own_sounds_ringtones_and_the_one_chosen_elsewhere(program, gcc_world, fb):
    fb.feedbackd()
    cello = fb.own_sound("Cello.oga")
    fb.own_sound(".hidden.oga")
    fb.own_sound("notes.txt")
    fb.music(gcc_world.home / "Music")
    (gcc_world.home / "Music/Ringtones").mkdir()
    (gcc_world.home / "Music/Ringtones/Ring.wav").write_bytes(WAV)
    fb.write_theme(theme(full=[sound("phone-incoming-call", "/elsewhere/Banjo.oga"),
                               sound("message-new-instant", fb.stereo("bell"))]))
    r = gcc_world.run(program, [theme_set("plinius"), SETUP, *open_sounds("Calls"), dump("calls"), back(),
                                back(), settle(), *open_sounds("Messages"), dump("messages")])
    s = r.dump("calls")
    assert group_rows(s, "Own Sounds") == ["Banjo", "Cello", "Ring"]
    assert checked(s) == ["Banjo"]
    # Only what was added here can be renamed or deleted
    own = s.within(type="AdwPreferencesGroup", text="Own Sounds")
    assert [r["text"] for r in own.find(type="AdwActionRow")
            if any(c.get("tooltip") == "Rename or Delete" for c in r.get("children", []))] == ["Cello"]
    # A system sound chosen is not repeated among the own ones
    assert group_rows(r.dump("messages"), "Own Sounds") == ["Cello", "Ring"]
    assert checked(r.dump("messages")) == ["Bell"]
    assert cello


def using_cello(fb):
    cello = fb.own_sound("Cello.oga")
    fb.write_theme(theme(full=[sound("phone-incoming-call", cello, "Ringtone"),
                               sound("message-new-instant", cello), sound("message-new-sms", cello)]))
    return cello


def test_rename_an_own_sound(program, gcc_world, fb):
    fb.feedbackd()
    cello = using_cello(fb)
    r = gcc_world.run(program, [theme_set("plinius"), SETUP, *open_sounds("Calls"), edit("Cello"), settle(),
                                dump("dialog"), set_text("  Viola ", type="AdwEntryRow", text="Name"),
                                respond("Rename"), settle(), dump("renamed")])
    d = r.dump("dialog")
    assert alert(d) == ("Edit Sound", None)
    assert d.one(type="AdwEntryRow", text="Name")["value"] == "Cello"
    viola = str(fb.own / "Viola.oga")
    assert sorted(p.name for p in fb.own.iterdir()) == ["Viola.oga"]
    assert group_rows(r.dump("renamed"), "Own Sounds") == ["Viola"]
    assert alert(r.dump("renamed")) is None
    # Every kind that used it follows
    assert fb.theme() == theme(full=[sound("phone-incoming-call", viola, "Ringtone"),
                                     sound("message-new-instant", viola), sound("message-new-sms", viola)])
    assert gcc_world.calls("signal", "SIGHUP")
    assert cello


@pytest.mark.parametrize("name", ["", "   ", ".Viola", "a/b"])
def test_rename_needs_a_usable_name(program, gcc_world, fb, name):
    fb.feedbackd()
    fb.own_sound("Cello.oga")
    r = gcc_world.run(program, [*open_sounds("Calls"), edit("Cello"), settle(),
                                set_text(name, type="AdwEntryRow", text="Name"), settle(), dump("dialog"),
                                set_text("Viola", type="AdwEntryRow", text="Name"), settle(), dump("fine")])
    assert r.dump("dialog").one(type="GtkButton", text="Rename").get("sensitive") is False
    assert r.dump("fine").one(type="GtkButton", text="Rename").get("sensitive", True) is True


def test_rename_to_the_same_name_or_cancel_changes_nothing(program, gcc_world, fb):
    fb.feedbackd()
    using_cello(fb)
    before = fb.theme_file.read_text()
    r = gcc_world.run(program, [theme_set("plinius"), SETUP, *open_sounds("Calls"), edit("Cello"), settle(),
                                respond("Rename"), settle(), edit("Cello"), settle(),
                                set_text("Viola", type="AdwEntryRow", text="Name"), respond("Cancel"), settle(),
                                dump("page")])
    assert sorted(p.name for p in fb.own.iterdir()) == ["Cello.oga"]
    assert fb.theme_file.read_text() == before
    assert group_rows(r.dump("page"), "Own Sounds") == ["Cello"]
    assert not gcc_world.calls("signal")


def test_rename_to_a_taken_name_numbers_it(program, gcc_world, fb):
    fb.feedbackd()
    fb.own_sound("Cello.oga")
    fb.own_sound("Viola.oga")
    r = gcc_world.run(program, [*open_sounds("Calls"), edit("Cello"), settle(),
                                set_text("Viola", type="AdwEntryRow", text="Name"), respond("Rename"), settle(),
                                dump("page")])
    assert sorted(p.name for p in fb.own.iterdir()) == ["Viola (2).oga", "Viola.oga"]
    # The renamed row keeps its place
    assert group_rows(r.dump("page"), "Own Sounds") == ["Viola (2)", "Viola"]
    # Nobody used it: the theme stays as it was
    assert not fb.theme_file.exists()


def test_rename_that_fails_says_why(program, gcc_world, fb):
    fb.feedbackd()
    fb.own_sound("Cello.oga")
    fb.own.chmod(0o555)
    try:
        r = gcc_world.run(program, [*open_sounds("Calls"), edit("Cello"), settle(),
                                    set_text("Viola", type="AdwEntryRow", text="Name"), respond("Rename"),
                                    settle(), dump("error")])
    finally:
        fb.own.chmod(0o755)
    assert alert(r.dump("error")) == ("Sound Not Renamed", "Permission denied")
    assert sorted(p.name for p in fb.own.iterdir()) == ["Cello.oga"]


def test_rename_without_a_free_name(program, gcc_world, fb):
    fb.feedbackd()
    fb.own_sound("Cello.oga")
    fb.own_sound("Viola.oga")
    for i in range(2, 1000):
        fb.own_sound(f"Viola ({i}).oga")
    r = gcc_world.run(program, [*open_sounds("Calls"), edit("Cello"), settle(),
                                set_text("Viola", type="AdwEntryRow", text="Name"), respond("Rename"), settle(),
                                dump("error")])
    assert alert(r.dump("error")) == ("Sound Not Renamed", "File exists")
    assert (fb.own / "Cello.oga").exists()


def test_delete_the_sound_chosen(program, gcc_world, fb):
    fb.feedbackd()
    using_cello(fb)
    r = gcc_world.run(program, [theme_set("plinius"), SETUP, *open_sounds("Calls"), dump("before"),
                                edit("Cello"), settle(), respond("Delete"), settle(), dump("after"),
                                theme_get()])
    assert checked(r.dump("before")) == ["Cello"]
    assert not (fb.own / "Cello.oga").exists()
    assert group_rows(r.dump("after"), "Own Sounds") == []
    # Back to the default, for every kind that used it, and not played
    assert checked(r.dump("after")) == ["Default"]
    assert not fb.theme_file.exists()
    assert r.value("theme") == "'default'"
    assert fb.plays() == []


def test_delete_a_sound_not_chosen_or_gone_already(program, gcc_world, fb):
    fb.feedbackd()
    cello = fb.own_sound("Cello.oga")
    fb.own_sound("Viola.oga")
    r = gcc_world.run(program, [*open_sounds("Calls"), {"op": "remove_file", "path": cello},
                                edit("Cello"), settle(), respond("Delete"), settle(), dump("after")])
    assert group_rows(r.dump("after"), "Own Sounds") == ["Viola"]
    assert checked(r.dump("after")) == ["Default"]
    assert alert(r.dump("after")) is None


def test_delete_that_fails_says_why(program, gcc_world, fb):
    fb.feedbackd()
    fb.own_sound("Cello.oga")
    fb.own.chmod(0o555)
    try:
        r = gcc_world.run(program, [*open_sounds("Calls"), edit("Cello"), settle(), respond("Delete"), settle(),
                                    dump("error")])
    finally:
        fb.own.chmod(0o755)
    assert alert(r.dump("error")) == ("Sound Not Deleted", "Permission denied")
    assert group_rows(r.dump("error"), "Own Sounds") == ["Cello"]


# ----- Adding a sound (the file chooser portal) -----

def portal(world, uri=None, response=0):
    results = "{'uris': <[%r]>}" % uri if uri else "@a{sv} {}"
    world.service("session", PORTAL, PORTAL_PATH, PORTAL_XML, properties={"version": "uint32 4"},
                  replies={"OpenFile": {"portal_request": {"response": response, "results": results}}})


ADD = [click(type="GtkButton", text="Add"), wait(300), settle()]


@pytest.mark.parametrize("name, data", [("pick.wav", WAV), ("pick.oga", OGG)])
def test_add_a_sound_copies_it_and_chooses_it(program, gcc_world, fb, name, data):
    fb.feedbackd()
    picked = gcc_world.tmp / "picked" / name
    picked.parent.mkdir()
    picked.write_bytes(data)
    portal(gcc_world, picked.as_uri())
    r = gcc_world.run(program, [*open_sounds("Calls"), *ADD, dump("added")])
    copy = fb.own / name
    assert copy.read_bytes() == data
    s = r.dump("added")
    assert group_rows(s, "Own Sounds") == ["pick"]
    assert checked(s) == ["pick"]
    assert s.has(type="GtkButton", tooltip="Rename or Delete")
    assert fb.theme() == theme(full=[sound("phone-incoming-call", str(copy), "Ringtone")])
    assert fb.plays()[0]["attrs"]["media.filename"] == str(copy)
    (c,) = gcc_world.calls(PORTAL, "OpenFile")
    assert c["args"][1] == "Choose a Sound"
    assert c["args"][2]["filters"][0][0] == "Ogg Vorbis or WAV"
    # Without a music folder the chooser starts where it likes
    assert "current_folder" not in c["args"][2]


def test_add_starts_in_the_music_folder_and_numbers_a_taken_name(program, gcc_world, fb):
    fb.feedbackd()
    fb.music(gcc_world.home / "Music")
    fb.own_sound("pick.wav")
    picked = gcc_world.home / "Music" / "pick.wav"
    picked.write_bytes(WAV)
    portal(gcc_world, picked.as_uri())
    r = gcc_world.run(program, [*open_sounds("Calls"), *ADD, dump("added")])
    assert sorted(p.name for p in fb.own.iterdir()) == ["pick (2).wav", "pick.wav"]
    assert checked(r.dump("added")) == ["pick (2)"]
    (c,) = gcc_world.calls(PORTAL, "OpenFile")
    assert bytes(c["args"][2]["current_folder"]) == str(gcc_world.home / "Music").encode() + b"\0"


@pytest.mark.parametrize("data", [b"ID3\x04" + bytes(60), b"RIFF", b"OggS" + bytes(24) + b"\x01opus  " + bytes(29),
                                  None])
def test_add_refuses_what_feedbackd_cannot_play(program, gcc_world, fb, data):
    fb.feedbackd()
    picked = gcc_world.tmp / "song.ogg"
    picked.write_bytes(data or OGG)
    if data is None:
        picked.chmod(0)
    portal(gcc_world, picked.as_uri())
    r = gcc_world.run(program, [*open_sounds("Calls"), *ADD, dump("refused")])
    assert alert(r.dump("refused")) == (
        "Sound Not Supported", "The phone plays ringtones and notification sounds in the Ogg Vorbis or WAV "
        "format. Convert the file to one of them first.")
    assert not fb.own.exists()
    assert not fb.theme_file.exists()


def test_add_without_room_for_own_sounds(program, gcc_world, fb):
    fb.feedbackd()
    (gcc_world.home / ".local/share/plinius").write_text("")
    picked = gcc_world.tmp / "pick.wav"
    picked.write_bytes(WAV)
    portal(gcc_world, picked.as_uri())
    r = gcc_world.run(program, [*open_sounds("Calls"), *ADD, dump("error")])
    assert alert(r.dump("error")) == ("Sound Not Added", "Not a directory")


def test_add_that_cannot_copy(program, gcc_world, fb):
    fb.feedbackd()
    fb.own.mkdir(parents=True)
    fb.own.chmod(0o555)
    picked = gcc_world.tmp / "pick.wav"
    picked.write_bytes(WAV)
    portal(gcc_world, picked.as_uri())
    try:
        r = gcc_world.run(program, [*open_sounds("Calls"), *ADD, dump("error")])
    finally:
        fb.own.chmod(0o755)
    heading, body = alert(r.dump("error"))
    assert heading == "Sound Not Added"
    assert "Permission denied" in body

