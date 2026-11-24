# The Streaming panel (cc-streaming-panel.c): Schwupp's settings (de.cais.Schwupp.Cast1 on the
# session bus), the defaults on the panel's page and the TVs below, each TV's page with its own
# values; changes go back through SetSetting, ResetDevice and ForgetDevice. Schwupp is a fake on
# the test's private session bus with the interface of its app/service.py.

import pytest

from gcc_harness import (build_panel, call, click, dump, gcc_session, gcc_world,  # noqa: F401
                         select, set_active, settle, wait, wait_for)

NAME = "de.cais.Schwupp"
PATH = "/de/cais/Schwupp/Cast"
IFACE = "de.cais.Schwupp.Cast1"
SERVICE_FILE = "/usr/share/dbus-1/services/de.cais.Schwupp.service"
# Schwupp 0.5.0 (commit 4f9edb98, the one the schwupp PKGBUILD pins), app/service.py
XML = f"""<node><interface name="{IFACE}">
 <method name="StartDiscovery"/>
 <method name="StopDiscovery"/>
 <method name="Connect"><arg name="id" type="s" direction="in"/></method>
 <method name="Disconnect"/>
 <method name="SubmitPin"><arg name="pin" type="s" direction="in"/></method>
 <method name="OpenSettings"><arg name="id" type="s" direction="in"/></method>
 <method name="GetSettings"><arg name="id" type="s" direction="in"/>
  <arg name="settings" type="a{{sv}}" direction="out"/></method>
 <method name="SetSetting"><arg name="id" type="s" direction="in"/>
  <arg name="key" type="s" direction="in"/><arg name="value" type="v" direction="in"/></method>
 <method name="ResetDevice"><arg name="id" type="s" direction="in"/></method>
 <method name="ForgetDevice"><arg name="id" type="s" direction="in"/></method>
 <property name="Devices" type="a(ssss)" access="read"/>
 <property name="ActiveDevice" type="s" access="read"/>
 <property name="State" type="s" access="read"/>
 <property name="Error" type="s" access="read"/>
 <property name="Discovering" type="b" access="read"/>
 <property name="KnownDevices" type="a(ssss)" access="read"/>
</interface></node>"""

GLUE = r'''
#include "gcc_driver.h"
#include "cc-streaming-panel.h"

static GVariant *
parameters (const char *text)
{
    g_autoptr(GError) error = NULL;
    GVariant *v = g_variant_parse (G_VARIANT_TYPE ("av"), text, NULL, NULL, &error);

    g_assert_no_error (error);
    return v;
}

CcPanel *
gcc_test_create_panel (GccTest *t)
{
    const char *text = gcc_test_param (t, "parameters");

    if (text)
        return g_object_new (CC_TYPE_STREAMING_PANEL, "window", t->window, "parameters", parameters (text), NULL);
    return g_object_new (CC_TYPE_STREAMING_PANEL, "window", t->window, NULL);
}

void
gcc_test_setup (GccTest *t)
{
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "static_init")) {
        cc_streaming_panel_static_init_func ();
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "parameters")) {
        /* "gnome-control-center streaming <id>" while the panel is open */
        g_object_set (t->panel, "parameters", parameters (json_array_get_string_element (args, 0)), NULL);
        json_builder_add_null_value (result);
        return TRUE;
    }
    return FALSE;
}
'''

NO_ANSWER = {"error": "org.freedesktop.DBus.Error.NoReply", "message": "Timed out"}


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("streaming"), "streaming",
                       ["cc-streaming-panel.c"], GLUE,
                       replacements={"cc-streaming-panel.c": [
                           (f'"{SERVICE_FILE}"', f'"{{root}}{SERVICE_FILE}"')]})


def q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def devices(*items):
    """a(ssss): (id, name, description, icon)."""
    if not items:
        return "@a(ssss) []"
    return "[" + ", ".join(f"({q(i)}, {q(n)}, {q(d)}, {q(ic)})" for i, n, d, ic in items) + "]"


def settings(**values):
    """GetSettings' answer: a{sv} of values (Python bool/int/str, lists of ints = ai, lists of
    str = as, lists of tuples = a(ssbs))."""
    parts = []
    for k, v in values.items():
        if isinstance(v, bool):
            t = "true" if v else "false"
        elif isinstance(v, int):
            t = f"int32 {v}"
        elif isinstance(v, str):
            t = q(v)
        elif v and isinstance(v[0], int):
            t = "@ai [" + ", ".join(str(x) for x in v) + "]"
        elif v and isinstance(v[0], tuple):
            t = "[" + ", ".join(f"({q(a)}, {q(b)}, {'true' if c else 'false'}, {q(d)})"
                                for a, b, c, d in v) + "]"
        elif v:
            t = "[" + ", ".join(q(x) for x in v) + "]"
        else:
            t = "@as []"
        parts.append(f"{q(k)}: <{t}>")
    return "({" + ", ".join(parts) + "},)" if parts else "(@a{sv} {},)"


DEFAULTS = settings(mirror_height=1080, mirror_bitrate_kbps=6000, mirror_fps=30, mirror_audio=True,
                    mirror_encoder="auto", encoders=["v4l2h264enc"], resolutions=[720, 1080, 2160])
LIVING = ("cast:living", "Living Room", "Chromecast", "video-display-symbolic")
BEDROOM = ("airplay:bed", "Bedroom", "AirPlay", "tv-symbolic")
SPEAKER = ("sonos:kitchen", "Kitchen", "Sonos", "audio-speakers-symbolic")


def schwupp(world, found=(), known=(), get=DEFAULTS, own=True, xml=XML, **replies):
    world.service("session", NAME, PATH, xml,
                  properties={"Devices": devices(*found), "KnownDevices": devices(*known)},
                  replies={"GetSettings": get, **replies}, own=own)


def calls(world, member):
    return [c["args"] for c in world.calls(NAME, member)]


def group(screen, title):
    return screen.within(type="AdwPreferencesGroup", text=title)


def values(screen, within="Defaults"):
    g = group(screen, within)
    res = g.one(type="AdwComboRow", text="Resolution")
    out = {"resolution": res.get("selected_text"), "resolutions": res.get("items", []),
           "bitrate": g.one(type="AdwSpinRow", text="Bitrate (kbit/s)")["value"],
           "fps": g.one(type="AdwSpinRow", text="Frame Rate (fps)")["value"],
           "sound": g.one(type="AdwSwitchRow", text="Sound")["active"]}
    enc = g.find(type="AdwComboRow", text="Video Encoder")
    out["encoder"] = (enc[0]["selected_text"], enc[0]["items"]) if enc else None
    return out


def tvs(screen):
    return group(screen, "TVs").rows()


def set_value(value, **sel):
    return {"op": "set_value", "target": sel, "value": value}


def open_tv(name):
    return [click(type="AdwActionRow", text=name), wait_for(type="AdwComboRow", text="Mirroring",
                                                             hidden=True), settle()]


def test_defaults_and_the_tvs(program, gcc_world):
    schwupp(gcc_world, found=[LIVING, SPEAKER], known=[LIVING, BEDROOM])
    r = gcc_world.run(program, [settle(), dump("page")])
    screen = r.dump("page")
    assert values(screen) == {"resolution": "1080p", "resolutions": ["720p", "1080p", "2160p (4K)"],
                              "bitrate": 6000.0, "fps": 30.0, "sound": True, "encoder": None}
    # Found ones first; a speaker has no settings; a known one not around says so
    assert tvs(screen) == [("Living Room", "Chromecast"),
                           ("Bedroom", "AirPlay · not found right now")]
    assert group(screen, "TVs").one(type="AdwActionRow", text="Bedroom")["children"][0]["icon"] == "tv-symbolic"
    assert calls(gcc_world, "StartDiscovery") == [[]]
    assert calls(gcc_world, "GetSettings") == [[""]]
    # Showing changes nothing
    assert not calls(gcc_world, "SetSetting")
    assert not r.warnings()


def test_no_tvs_shows_the_search(program, gcc_world):
    schwupp(gcc_world, found=[SPEAKER])
    r = gcc_world.run(program, [settle(), dump("page")])
    assert tvs(r.dump("page")) == [("Searching for TVs…", "TVs in the same network show up here")]


def test_encoders_to_choose_from(program, gcc_world):
    schwupp(gcc_world, get=settings(mirror_height=720, mirror_bitrate_kbps=4000, mirror_fps=25,
                                    mirror_audio=False, mirror_encoder="x264enc",
                                    encoders=["v4l2h264enc", "x264enc"], resolutions=[720, 1080]))
    r = gcc_world.run(program, [settle(), dump("page")])
    assert values(r.dump("page")) == {
        "resolution": "720p", "resolutions": ["720p", "1080p"], "bitrate": 4000.0, "fps": 25.0,
        "sound": False, "encoder": ("x264enc", ["Automatic", "v4l2h264enc", "x264enc"])}


def test_missing_values_take_schwupps_defaults(program, gcc_world):
    schwupp(gcc_world, get=settings(mirror_encoder="gone", encoders=["a", "b"]))
    r = gcc_world.run(program, [settle(), dump("page")])
    v = values(r.dump("page"))
    assert (v["bitrate"], v["fps"], v["sound"]) == (6000.0, 30.0, True)
    assert v["resolutions"] == []
    # An encoder that is not there any more: Automatic
    assert v["encoder"] == ("Automatic", ["Automatic", "a", "b"])
    assert not calls(gcc_world, "SetSetting")


@pytest.mark.parametrize("step, sent", [
    (select(2, type="AdwComboRow", text="Resolution"), ["mirror_height", 2160]),
    (set_value(8000, type="AdwSpinRow", text="Bitrate (kbit/s)"), ["mirror_bitrate_kbps", 8000]),
    (set_value(60, type="AdwSpinRow", text="Frame Rate (fps)"), ["mirror_fps", 60]),
    (set_active(False, type="AdwSwitchRow", text="Sound"), ["mirror_audio", False]),
    (select(2, type="AdwComboRow", text="Video Encoder"), ["mirror_encoder", "x264enc"]),
    (select(0, type="AdwComboRow", text="Video Encoder"), ["mirror_encoder", "auto"]),
])
def test_changing_a_default(program, gcc_world, step, sent):
    schwupp(gcc_world, get=settings(mirror_height=1080, mirror_encoder="v4l2h264enc",
                                    encoders=["v4l2h264enc", "x264enc"], resolutions=[720, 1080, 2160]))
    r = gcc_world.run(program, [settle(), step, settle()])
    assert calls(gcc_world, "SetSetting") == [["", *sent]]
    assert not r.warnings()


def test_refused_change_is_logged(program, gcc_world):
    schwupp(gcc_world, SetSetting={"error": "org.freedesktop.DBus.Error.InvalidArgs",
                                   "message": "out of range"})
    r = gcc_world.run(program, [settle(), set_active(False, type="AdwSwitchRow", text="Sound"),
                                settle()])
    assert [m["message"] for m in r.warnings()] == [
        "Changing a streaming setting of '' failed: GDBus.Error:org.freedesktop.DBus.Error."
        "InvalidArgs: out of range"]


def test_defaults_without_an_answer(program, gcc_world):
    schwupp(gcc_world, get=NO_ANSWER)
    r = gcc_world.run(program, [settle(), dump("page")])
    screen = r.dump("page")
    assert screen.has(type="GtkLabel", text="Schwupp does not answer")
    assert [m["message"] for m in r.warnings()] == [
        "Schwupp does not answer: GDBus.Error:org.freedesktop.DBus.Error.NoReply: Timed out"]


def test_discovery_refused(program, gcc_world):
    schwupp(gcc_world, StartDiscovery={"error": "org.freedesktop.DBus.Error.Failed",
                                       "message": "no network"})
    r = gcc_world.run(program, [settle(), dump("page")])
    assert r.dump("page").has(type="GtkLabel", text="Schwupp does not answer")
    assert [m["message"] for m in r.warnings()] == [
        "Schwupp does not answer: GDBus.Error:org.freedesktop.DBus.Error.Failed: no network"]


def test_tv_page_with_engines(program, gcc_world):
    tv = settings(name="Living Room", description="Chromecast on the shelf", kind="chromecast",
                  custom=True, mirror_engine="native", mirror_height=720, resolutions=[720, 1080],
                  engines=[("native", "Cast", True, ""), ("hls", "HLS", False, "needs ffmpeg")])
    schwupp(gcc_world, found=[LIVING], get=[DEFAULTS, tv, tv])
    r = gcc_world.run(program, [settle(), *open_tv("Living Room"), dump("tv")])
    screen = r.dump("tv")
    page = screen.one(type="AdwNavigationPage", text="Living Room")
    s = type(screen)(page.get("children", []))
    engine = s.one(type="AdwComboRow", text="Mirroring")
    assert (engine["selected_text"], engine["items"]) == ("Cast", ["Cast", "HLS (not available)"])
    assert engine["subtitle"] == "How the screen gets to this TV"
    assert s.has(type="AdwPreferencesGroup", subtitle="Chromecast on the shelf")
    assert values(s, "Picture and Sound")["resolution"] == "720p"
    # Own values: back to the defaults is possible; a TV only found is not forgotten
    assert s.one(type="AdwButtonRow", text="Use the Defaults").get("sensitive", True)
    assert not s.has(type="AdwButtonRow", text="Forget This TV")
    assert not s.has(type="AdwActionRow", text="Video Playback")
    # The page's values are asked for with the TV's id
    assert calls(gcc_world, "GetSettings")[-1] == ["cast:living"]
    assert len(calls(gcc_world, "GetSettings")) == 3
    assert not calls(gcc_world, "SetSetting")
    assert not r.warnings()


def test_tv_page_changes_the_tvs_own_values(program, gcc_world):
    tv = settings(name="Living Room", kind="chromecast", custom=False, mirror_engine="hls",
                  engines=[("native", "Cast", True, ""), ("hls", "HLS", True, "")])
    schwupp(gcc_world, found=[LIVING], get=[DEFAULTS, tv])
    r = gcc_world.run(program, [settle(), *open_tv("Living Room"), dump("before"),
                                select(0, type="AdwComboRow", text="Mirroring"), settle(),
                                set_value(30, type="AdwSpinRow", text="Frame Rate (fps)",
                                          within={"type": "AdwNavigationPage", "text": "Living Room"}),
                                settle(), dump("after")])
    before = r.dump("before")
    assert before.one(type="AdwButtonRow", text="Use the Defaults").get("sensitive") is False
    assert calls(gcc_world, "SetSetting") == [["cast:living", "mirror_engine", "native"]]
    # The TV follows the defaults no more
    assert r.dump("after").one(type="AdwButtonRow", text="Use the Defaults").get("sensitive", True)


def test_one_engine_is_no_choice(program, gcc_world):
    tv = settings(name="Living Room", kind="chromecast", mirror_engine="native",
                  engines=[("native", "Cast", True, "")])
    schwupp(gcc_world, found=[LIVING], get=[DEFAULTS, tv])
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Living Room"),
                                wait_for(type="AdwNavigationPage", text="Living Room"), settle(),
                                dump("tv")])
    assert not r.dump("tv").has(type="AdwComboRow", text="Mirroring")


def test_reset_shows_the_defaults_again(program, gcc_world):
    own = settings(name="Living Room", kind="chromecast", custom=True, mirror_fps=60)
    after = settings(name="Living Room", kind="chromecast", custom=False, mirror_fps=30)
    schwupp(gcc_world, found=[LIVING], get=[DEFAULTS, own, own, after])
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Living Room"),
                                wait_for(type="AdwNavigationPage", text="Living Room"), settle(),
                                click(type="AdwButtonRow", text="Use the Defaults"), settle(),
                                dump("reset")])
    assert calls(gcc_world, "ResetDevice") == [["cast:living"]]
    screen = r.dump("reset")
    assert values(screen, "Picture and Sound")["fps"] == 30.0
    assert screen.one(type="AdwButtonRow", text="Use the Defaults").get("sensitive") is False
    assert not calls(gcc_world, "SetSetting")


def test_reset_refused(program, gcc_world):
    own = settings(name="Living Room", kind="chromecast", custom=True)
    schwupp(gcc_world, found=[LIVING], get=[DEFAULTS, own, own],
            ResetDevice={"error": "org.freedesktop.DBus.Error.Failed", "message": "busy"})
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Living Room"),
                                wait_for(type="AdwNavigationPage", text="Living Room"), settle(),
                                click(type="AdwButtonRow", text="Use the Defaults"), settle(),
                                dump("page")])
    assert len(calls(gcc_world, "GetSettings")) == 3
    assert [m["message"] for m in r.warnings()] == [
        "Schwupp does not answer: GDBus.Error:org.freedesktop.DBus.Error.Failed: busy"]


def test_leaving_while_reset_waits(program, gcc_world):
    own = settings(name="Living Room", kind="chromecast", custom=True)
    schwupp(gcc_world, found=[LIVING], get=[DEFAULTS, own, own],
            ResetDevice={"delay_ms": 2000, "reply": "()"})
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Living Room"),
                                wait_for(type="AdwNavigationPage", text="Living Room"), settle(),
                                click(type="AdwButtonRow", text="Use the Defaults"), settle()])
    assert calls(gcc_world, "ResetDevice") == [["cast:living"]]
    # The window went before the answer: no values asked again, no warning
    assert len(calls(gcc_world, "GetSettings")) == 3
    assert not r.warnings()


def test_forget_a_known_tv(program, gcc_world):
    tv = settings(name="Bedroom", kind="dlna")
    schwupp(gcc_world, known=[BEDROOM], get=[DEFAULTS, tv])
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Bedroom"),
                                wait_for(type="AdwNavigationPage", text="Bedroom"), settle(),
                                dump("tv"), click(type="AdwButtonRow", text="Forget This TV"),
                                settle(), dump("after")])
    forget = r.dump("tv").one(type="AdwButtonRow", text="Forget This TV")
    assert "destructive-action" in forget.get("classes", [])
    assert calls(gcc_world, "ForgetDevice") == [["airplay:bed"]]
    # Back on the panel's page
    assert r.dump("after").has(type="AdwPreferencesGroup", text="Defaults")
    assert not r.dump("after").has(type="AdwNavigationPage", text="Bedroom")


def test_airplay_tv_that_refused_videos(program, gcc_world):
    tv = settings(name="Bedroom", kind="airplay", airplay_video=False)
    schwupp(gcc_world, found=[BEDROOM], get=[DEFAULTS, tv])
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Bedroom"),
                                wait_for(type="AdwNavigationPage", text="Bedroom"), settle(),
                                dump("tv"), click(type="GtkButton", text="Try Again"), settle(),
                                dump("after")])
    row = r.dump("tv").one(type="AdwActionRow", text="Video Playback")
    assert row["subtitle"] == "This TV refused videos, they are no longer offered to it"
    assert calls(gcc_world, "SetSetting") == [["airplay:bed", "airplay_video", True]]
    assert not r.dump("after").has(type="AdwActionRow", text="Video Playback")


def test_airplay_tv_that_takes_videos(program, gcc_world):
    tv = settings(name="Bedroom", kind="airplay")
    schwupp(gcc_world, found=[BEDROOM], get=[DEFAULTS, tv])
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Bedroom"),
                                wait_for(type="AdwNavigationPage", text="Bedroom"), settle(),
                                dump("tv")])
    assert not r.dump("tv").has(type="AdwActionRow", text="Video Playback")


def test_tv_page_without_an_answer_for_its_values(program, gcc_world):
    tv = settings(name="Living Room", kind="chromecast")
    schwupp(gcc_world, found=[LIVING], get=[DEFAULTS, tv, NO_ANSWER])
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Living Room"),
                                wait_for(type="AdwNavigationPage", text="Living Room"), settle(),
                                dump("tv")])
    assert r.dump("tv").has(type="GtkLabel", text="Schwupp does not answer")
    assert [m["message"] for m in r.warnings()] == [
        "Schwupp does not answer: GDBus.Error:org.freedesktop.DBus.Error.NoReply: Timed out"]


def test_tv_gone_before_its_page_opens(program, gcc_world):
    schwupp(gcc_world, found=[LIVING], get=[DEFAULTS, {"error": "de.cais.Schwupp.Error.NotFound",
                                                       "message": "unknown device"}])
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Living Room"), settle(),
                                dump("page")])
    # The panel itself stays
    assert not r.dump("page").has(type="AdwNavigationPage", text="Living Room")
    assert r.dump("page").has(type="AdwPreferencesGroup", text="Defaults")
    assert not r.warnings()


def test_page_named_after_the_id_without_a_name(program, gcc_world):
    schwupp(gcc_world, found=[LIVING], get=[DEFAULTS, settings(kind="chromecast")])
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Living Room"),
                                wait_for(type="AdwNavigationPage", text="cast:living"), settle(),
                                dump("tv")])
    assert r.dump("tv").has(type="AdwNavigationPage", text="cast:living")


def test_opened_with_a_tv_from_the_command_line(program, gcc_world):
    tv = settings(name="Living Room", kind="chromecast")
    schwupp(gcc_world, known=[LIVING], get=[DEFAULTS, tv])
    r = gcc_world.run(program, [settle(), dump("tv")],
                      params={"parameters": "[<'cast:living'>]"})
    screen = r.dump("tv")
    assert screen.has(type="AdwNavigationPage", text="Living Room")
    assert screen.has(type="AdwButtonRow", text="Forget This TV")
    assert calls(gcc_world, "GetSettings")[0] == [""]
    assert calls(gcc_world, "GetSettings")[-1] == ["cast:living"]
    assert len(calls(gcc_world, "GetSettings")) == 3
    assert not r.warnings()


@pytest.mark.parametrize("params", ["@av []", "[<''>]", "[<int32 3>]"])
def test_parameters_without_a_tv_open_the_panel(program, gcc_world, params):
    schwupp(gcc_world, found=[LIVING])
    r = gcc_world.run(program, [settle(), dump("page")], params={"parameters": params})
    assert not r.dump("page").has(type="AdwNavigationPage", text="Living Room")
    assert calls(gcc_world, "GetSettings") == [[""]]


def test_tv_asked_for_while_the_panel_is_open(program, gcc_world):
    tv = settings(name="Living Room", kind="chromecast")
    schwupp(gcc_world, found=[LIVING], get=[DEFAULTS, tv])
    r = gcc_world.run(program, [settle(), call("parameters", "[<'cast:living'>]"),
                                wait_for(type="AdwNavigationPage", text="Living Room"), settle(),
                                dump("tv")])
    assert r.dump("tv").has(type="AdwNavigationPage", text="Living Room")
    assert calls(gcc_world, "GetSettings")[-1] == ["cast:living"]
    assert len(calls(gcc_world, "GetSettings")) == 3


def test_tvs_follow_schwupp(program, gcc_world):
    schwupp(gcc_world)
    ctl = gcc_world.control("session")
    r = gcc_world.run(program, [settle(), dump("none"),
                                ctl.set_property(PATH, IFACE, "Devices", devices(LIVING)), settle(),
                                dump("found"),
                                ctl.set_property(PATH, IFACE, "KnownDevices", devices(BEDROOM)), settle(),
                                dump("known"),
                                ctl.set_property(PATH, IFACE, "State", "'streaming'"), settle(),
                                dump("state")])
    assert tvs(r.dump("none")) == [("Searching for TVs…", "TVs in the same network show up here")]
    assert tvs(r.dump("found")) == [("Living Room", "Chromecast")]
    assert tvs(r.dump("known")) == [("Living Room", "Chromecast"),
                                    ("Bedroom", "AirPlay · not found right now")]
    assert tvs(r.dump("state")) == tvs(r.dump("known"))


def test_tv_names_are_not_markup(program, gcc_world):
    schwupp(gcc_world, found=[("x", "Tom & <Jerry>", "DLNA", "tv-symbolic")])
    r = gcc_world.run(program, [settle(), dump("page")])
    assert tvs(r.dump("page")) == [("Tom & <Jerry>", "DLNA")]


def test_schwupp_quits_and_starts_again(program, gcc_world):
    schwupp(gcc_world, found=[LIVING])
    ctl = gcc_world.control("session")
    r = gcc_world.run(program, [settle(), ctl.release(NAME), settle(), dump("gone"),
                                ctl.own(NAME), settle(200), dump("back")])
    assert tvs(r.dump("gone")) == [("Searching for TVs…", "TVs in the same network show up here")]
    assert tvs(r.dump("back")) == [("Living Room", "Chromecast")]
    # The defaults are read again when it is back
    assert calls(gcc_world, "GetSettings") == [[""], [""]]


def test_schwupp_not_running_yet(program, gcc_world):
    schwupp(gcc_world, found=[LIVING], own=False)
    r = gcc_world.run(program, [settle(), dump("page")])
    screen = r.dump("page")
    assert tvs(screen) == [("Searching for TVs…", "TVs in the same network show up here")]
    assert not calls(gcc_world, "GetSettings")


def test_leaving_the_panel_ends_the_search(program, gcc_world):
    schwupp(gcc_world)
    gcc_world.run(program, [settle()])
    assert calls(gcc_world, "StartDiscovery") == [[]]
    assert calls(gcc_world, "StopDiscovery") == [[]]


def test_leaving_while_a_change_is_sent(program, gcc_world):
    schwupp(gcc_world, SetSetting={"delay_ms": 2000, "reply": "()"})
    r = gcc_world.run(program, [settle(), set_active(False, type="AdwSwitchRow", text="Sound")])
    assert calls(gcc_world, "SetSetting") == [["", "mirror_audio", False]]
    assert not r.warnings()


def test_without_a_session_bus(program, gcc_world):
    schwupp(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")], session_bus=False)
    screen = r.dump("page")
    assert tvs(screen) == [("Searching for TVs…", "TVs in the same network show up here")]
    assert [m["message"] for m in r.warnings()][0].startswith("Cannot reach Schwupp: ")
    # Changes have nowhere to go
    r2 = gcc_world.run(program, [settle(), set_active(False, type="AdwSwitchRow", text="Sound"),
                                 settle()], session_bus=False)
    assert not calls(gcc_world, "SetSetting")
    assert r2.returncode == 0


@pytest.mark.parametrize("installed, shown", [(True, "visible"), (False, "hidden")])
def test_listed_only_with_schwupp(program, gcc_world, installed, shown):
    f = program.root / SERVICE_FILE.lstrip("/")
    if installed:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("[D-BUS Service]\nName=de.cais.Schwupp\n")
    elif f.exists():
        f.unlink()
    schwupp(gcc_world)
    r = gcc_world.run(program, [call("static_init")])
    assert r.visibility == {"streaming": shown}


def test_the_first_question_for_a_tv_names_it(program, gcc_world):
    tv = settings(name="Living Room", kind="chromecast")
    schwupp(gcc_world, found=[LIVING], get=[DEFAULTS, tv])
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Living Room"),
                                wait_for(type="AdwNavigationPage", text="Living Room"), settle()])
    # Schwupp answers an unknown id with an error, and the page would not open
    assert calls(gcc_world, "GetSettings") == [[""], ["cast:living"], ["cast:living"]]
    assert not r.warnings()


def test_error_on_a_tvs_page_shows_there(program, gcc_world):
    own = settings(name="Living Room", kind="chromecast", custom=True)
    schwupp(gcc_world, found=[LIVING], get=[DEFAULTS, own, own],
            ResetDevice={"error": "org.freedesktop.DBus.Error.Failed", "message": "busy"})
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Living Room"),
                                wait_for(type="AdwNavigationPage", text="Living Room"), settle(),
                                click(type="AdwButtonRow", text="Use the Defaults"), settle(),
                                dump("tv"), {"op": "back"}, settle(), dump("panel")])
    assert r.dump("tv").has(type="GtkLabel", text="Schwupp does not answer")
    # It was about the TV's page, it does not wait on the panel's
    assert not r.dump("panel").has(type="GtkLabel", text="Schwupp does not answer")


def test_tv_without_its_way_of_mirroring_gets_the_first(program, gcc_world):
    # An answer without "mirror_engine" (an older Schwupp, a TV never set): the first way shows
    tv = settings(name="Living Room", kind="chromecast",
                  engines=[("native", "Cast", True, ""), ("hls", "HLS", False, "")])
    schwupp(gcc_world, found=[LIVING], get=[DEFAULTS, tv])
    r = gcc_world.run(program, [settle(), *open_tv("Living Room"), dump("tv")])
    row = r.dump("tv").one(type="AdwComboRow", text="Mirroring")
    assert row["selected_text"] == "Cast"
    assert row["items"] == ["Cast", "HLS (not available)"]
    assert not calls(gcc_world, "SetSetting")
    assert not r.warnings()


def test_schwupp_without_known_tvs(program, gcc_world):
    # Before Schwupp remembered TVs (no KnownDevices): found ones only, none to forget
    old = XML.replace(' <property name="KnownDevices" type="a(ssss)" access="read"/>\n', "")
    assert old != XML
    tv = settings(name="Living Room", kind="chromecast")
    schwupp(gcc_world, found=[LIVING], get=[DEFAULTS, tv], xml=old)
    r = gcc_world.run(program, [settle(), dump("page"), click(type="AdwActionRow", text="Living Room"),
                                wait_for(type="AdwNavigationPage", text="Living Room"), settle(),
                                dump("tv")])
    assert tvs(r.dump("page")) == [("Living Room", "Chromecast")]
    assert r.dump("tv").has(type="AdwButtonRow", text="Use the Defaults")
    assert not r.dump("tv").has(type="AdwButtonRow", text="Forget This TV")
    assert not r.warnings()
