# audio-hal-bridge: what it follows in PipeWire's registry and what it decides from it. Voice
# streams on the microphone (echo cancellation and the speakerphone on or off), Bluetooth
# headsets (their profiles, nodes and the default sink), the headset's microphone as a source of
# the bridge, and hands-free through the HAL: profile switch, the hold stream, the codec, the HAL
# streams on SCO, the release afterwards, timeouts and headsets that go away. Registry events
# come from the stand-in PipeWire (c/dhw_audio_harness.c), the main loop runs with "idle".

import pytest

from dhw_audio_helpers import MIC, SCO_MIC, audio, kv  # noqa: F401 (fixture)

DEVICE = "global:50:Device:device.api=bluez5;media.class=Audio/Device"
PROFILES = ["dev-param:50:EnumProfile:1:a2dp-sink", "dev-param:50:EnumProfile:2:headset-head-unit",
            "dev-param:50:EnumProfile:3:headset-head-unit-cvsd", "dev-param:50:Profile:1:a2dp-sink"]
SINK = "global:60:Node:node.name=bluez_output.AA.1;media.class=Audio/Sink"
METADATA = ["global:70:Metadata:metadata.name=default", 'meta:70:0:default.audio.sink:{"name":"bluez_output.AA.1"}']


def sink_info(profile="a2dp-sink", codec="sbc", node=60, description="Headset X"):
    return (f"node-info:{node}:device.id=50;node.description={description};api.bluez5.profile={profile};"
            f"api.bluez5.codec={codec}")


# The HAL has no stereo input on SCO
PRE = [f"fail:open_in:{SCO_MIC:#x}:0xc"]
HEADSET = [*PRE, "setup", DEVICE, *PROFILES, SINK, sink_info(), *METADATA, "idle"]
# A call goes to the headset until the HAL is on SCO
TO_SCO = ["call:1:0", "idle", sink_info("headset-head-unit", ""), "idle", sink_info("headset-head-unit", "msbc"),
          "idle", "state:audio-hal-bridge.sco-hold:streaming", "idle"]


def bt(out):
    """The bridge's state after each "bt" command."""
    return [kv(line.replace(" ", "\n")) for line in out.splitlines() if line.startswith("bt sco_state=")]


def lines_of(out, *prefixes):
    return [line for line in out.splitlines() if line.startswith(prefixes)]


# ---- Voice streams on the microphone ----

STREAM = "global:80:Node:media.class=Stream/Input/Audio;client.id=5"


def test_voice_stream_turns_echo_cancellation_on(audio):
    out = audio("setup", "global:80:Node:media.class=Stream/Input/Audio;client.id=5;media.role=Communication",
                "link:90:hal_input:80", "voice", "remove:90", "voice")
    assert "log echo cancellation on" in out and "log speakerphone on" in out
    assert lines_of(out, "voice ") == ["voice aec_on=1 speakerphone=1 mic_links=1",
                                       "voice aec_on=0 speakerphone=0 mic_links=0"]


def test_voice_role_learned_from_the_node_info(audio):
    out = audio("setup", STREAM, "link:90:hal_input:80", "voice", "node-info:80:media.role=Phone", "voice",
                "node-info:80", "voice")
    assert lines_of(out, "voice ") == ["voice aec_on=0 speakerphone=0 mic_links=1"] + [
        "voice aec_on=1 speakerphone=1 mic_links=1"] * 2


@pytest.mark.parametrize("player,info,on", [
    ("client.id=5", [], 1),                           # same client, processes unknown
    ("client.id=6", [], 0),                           # another app
    # pipewire-pulse: one client for all apps, the process decides
    ("client.id=5", ["node-info:80:application.process.id=42", "node-info:81:application.process.id=42"], 1),
    ("client.id=5", ["node-info:80:application.process.id=42", "node-info:81:application.process.id=43"], 0),
])
def test_app_that_also_plays_is_a_conversation(audio, player, info, on):
    out = audio("setup", STREAM, f"global:81:Node:media.class=Stream/Output/Audio;{player}", *info,
                "link:90:hal_input:80", "voice")
    assert lines_of(out, "voice ")[-1] == f"voice aec_on={on} speakerphone={on} mic_links=1"


def test_no_echo_cancellation_without_a_canceller(audio):
    out = audio("setup", "aec:null", "global:80:Node:media.class=Stream/Input/Audio;client.id=5;media.role=Phone",
                "link:90:hal_input:80", "voice")
    assert lines_of(out, "voice ") == ["voice aec_on=0 speakerphone=0 mic_links=1"]


def test_no_speakerphone_in_a_mobile_call(audio):
    out = audio("setup", "route:0:0:0:1:1:0:0",
                "global:80:Node:media.class=Stream/Input/Audio;client.id=5;media.role=Communication",
                "link:90:hal_input:80", "voice")
    assert lines_of(out, "voice ") == ["voice aec_on=1 speakerphone=0 mic_links=1"]


@pytest.mark.parametrize("props", [
    "media.class=Stream/Input/Audio;media.role=Communication",                           # no client
    "media.class=Stream/Input/Audio;client.id=5;media.role=Communication;node.link-group=x",  # a loopback
    "media.class=Video/Source;client.id=5;media.role=Communication",
    "client.id=5;media.role=Communication",
])
def test_not_an_app_stream(audio, props):
    out = audio("setup", f"global:80:Node:{props}", "link:90:hal_input:80", "voice")
    assert "bind 80" not in out
    assert lines_of(out, "voice ") == ["voice aec_on=0 speakerphone=0 mic_links=1"]


def test_registry_entries_ignored(audio):
    out = audio("setup", "global-noprops:80:Node", "global:81:Port:port.name=x", "link:90:200:80",
                "global:91:Link:link.output.node=200", "voice")
    # No properties, another type, a link from elsewhere or half a link: nothing followed
    assert "bind" not in out
    assert lines_of(out, "voice ") == ["voice aec_on=0 speakerphone=0 mic_links=0"]


def test_links_removed_out_of_order(audio):
    out = audio("setup", "global:80:Node:media.class=Stream/Input/Audio;client.id=5;media.role=Phone", STREAM.replace("80", "81"),
                "link:90:hal_input:81", "link:91:hal_input:81", "link:92:hal_input:80", "remove:90", "voice",
                "remove:92", "voice")
    assert lines_of(out, "voice ") == ["voice aec_on=1 speakerphone=1 mic_links=2",
                                       "voice aec_on=0 speakerphone=0 mic_links=1"]


def test_stream_and_link_tables_full(audio):
    out = audio("setup", "globals:100:64:Node:media.class=Stream/Input/Audio;client.id=5",
                "global:300:Node:media.class=Stream/Input/Audio;client.id=5;media.role=Phone",
                "links:400:64:hal_input:100", "link:500:hal_input:300", "voice", "remove:100", "remove:400",
                "global:301:Node:media.class=Stream/Input/Audio;client.id=5;media.role=Phone", "link:501:hal_input:301",
                "voice")
    # 64 streams and links are followed; the 65th of each is not
    assert "bind 300" not in out
    voice = lines_of(out, "voice ")
    assert voice[0] == "voice aec_on=0 speakerphone=0 mic_links=64"
    # Room again after a removal
    assert "bind 301 Node" in out and voice[1] == "voice aec_on=1 speakerphone=1 mic_links=64"


def test_stream_without_proxy(audio):
    out = audio("setup", "fail:bind:80", "global:80:Node:media.class=Stream/Input/Audio;client.id=5;media.role=Phone",
                "link:90:hal_input:80", "voice", "remove:80", "voice")
    # Followed by its registry properties alone, and forgotten when it goes
    assert lines_of(out, "voice ") == ["voice aec_on=1 speakerphone=1 mic_links=1",
                                       "voice aec_on=0 speakerphone=0 mic_links=1"]
    assert "proxy-destroy 80" not in out


# ---- Bluetooth devices and the default sink ----

def test_headset_source_appears(audio):
    out = audio(*HEADSET, "bt")
    assert "subscribe 50 EnumProfile,Profile" in out
    assert "node.name=hal_headset_input node.description=Headset X device.icon-name=audio-headset" in out
    assert "audio-hal-bridge: headset source Headset X for bluez_output.AA.1" in out
    assert bt(out)[-1]["source"] == "1" and bt(out)[-1]["headset"] == "Headset"
    # Neither a call nor a recording: no hands-free
    assert "set-profile" not in out and bt(out)[-1]["sco_state"] == "0"


def test_headset_source_needs_hands_free(audio):
    out = audio("setup", DEVICE, "dev-param:50:EnumProfile:1:a2dp-sink", "dev-param:50:Profile:1:a2dp-sink", SINK,
                sink_info(), *METADATA, "idle", "bt",
                # The profiles changed: asked again, hands-free is offered now
                "dev-info:50:EnumProfile=r,Profile=r,Props=r,EnumProfile=-", "dev-info:50",
                "dev-param:50:EnumProfile:2:headset-head-unit", "idle", "bt")
    states = bt(out)
    assert states[0]["source"] == "0" and states[1]["source"] == "1"
    assert lines_of(out, "enum-params") == ["enum-params 50 EnumProfile", "enum-params 50 Profile"]


@pytest.mark.parametrize("cmds", [
    ["dev-param:50:EnumProfile:2"],                         # no profile name
    ["dev-param:50:EnumProfile:2:headset-head-unit-msbc"],  # a profile the bridge does not use
])
def test_device_params_ignored(audio, cmds):
    out = audio("setup", DEVICE, "dev-param:50:EnumProfile:1:a2dp-sink", *cmds, SINK, sink_info(), *METADATA,
                "idle", "bt")
    assert bt(out)[-1]["source"] == "0"


def test_headset_source_needs_a_name(audio):
    out = audio("setup", DEVICE, *PROFILES, SINK, "node-info:60:device.id=50", "node-info:60", *METADATA, "idle",
                "bt", sink_info(), "idle", "bt")
    assert [s["source"] for s in bt(out)] == ["0", "1"]


def test_headset_source_of_any_hands_free_headset(audio):
    # Not the default sink's: still the one headset that can do hands-free
    out = audio("setup", DEVICE, *PROFILES, SINK, sink_info(), "idle", "bt")
    assert bt(out)[-1]["source"] == "1"


@pytest.mark.parametrize("fail,destroyed", [("stream_connect:hal_headset_input", True),
                                            ("stream_new:hal_headset_input", False)])
def test_headset_source_that_cannot_be_made(audio, fail, destroyed):
    out = audio(f"fail:{fail}", *HEADSET, "bt")
    assert bt(out)[-1]["source"] == "0"
    assert ("stream-destroy hal_headset_input" in out) == destroyed


def test_headset_source_stays_through_a_profile_change(audio):
    out = audio(*HEADSET, "remove:60", "idle", "bt", SINK.replace("60", "61"), sink_info(node=61), "idle", "bt",
                "remove:50", "idle", "bt")
    states = bt(out)
    # The nodes are replaced, the device stays: the source stays; with the device it goes
    assert [s["source"] for s in states] == ["1", "1", "0"]
    assert out.count("stream-new hal_headset_input") == 1
    assert "audio-hal-bridge: headset source Headset X removed" in out


def test_bluetooth_registry_entries(audio):
    out = audio("setup", "global:40:Device:device.api=alsa;media.class=Audio/Device",
                "global:41:Device:device.api=bluez5;media.class=Video/Device",
                "global:42:Device:media.class=Audio/Device",
                "global:43:Node:node.name=bluez_input.AA.0;media.class=Stream/Output/Audio",
                "global:44:Node:node.name=bluez_input.AA.0",
                "global:45:Node:node.name=bluez_input.AA.0;media.class=Audio/Source",
                "global:46:Metadata:metadata.name=settings", "global:47:Metadata", *METADATA,
                "global:71:Metadata:metadata.name=default")
    # Only Bluetooth audio devices, their sinks and sources (not a loopback's inner stream), and
    # the one "default" metadata
    assert lines_of(out, "bind") == ["bind 45 Node", "bind 70 Metadata"]


def test_bluetooth_tables_full(audio):
    out = audio("setup", "globals:100:9:Device:device.api=bluez5;media.class=Audio/Device",
                "globals:200:17:Node:node.name=bluez_output.X;media.class=Audio/Sink")
    assert "bind 107 Device" in out and "bind 108 Device" not in out
    assert "bind 215 Node" in out and "bind 216 Node" not in out


def test_bluetooth_proxies_that_cannot_be_bound(audio):
    out = audio("setup", "fail:bind:50,bind:60,bind:70", DEVICE, SINK, *METADATA, "global:71:Metadata:metadata.name=default",
                "idle", "bt", "remove:50", "remove:60", "bt")
    assert bt(out)[0]["source"] == "0"
    # The second "default" is taken when the first could not be bound
    assert "bind 71 Metadata" in out
    assert "proxy-destroy" not in out


@pytest.mark.parametrize("value,port", [
    ('{"name":"hal_earpiece"}', "hal_earpiece"),
    ('{"name":"hal_headphones"}', "hal_headphones"),
    ('{"name":"hal_output"}', "hal_output"),
    ('{"name":"bluez_output.AA.1"}', "none"),   # a headset sets its own volume
    ('{"other":1}', "hal_output"),              # no name: as if none, the speaker
    ("-", "hal_output"),                        # cleared
])
def test_default_sink(audio, value, port):
    out = audio("setup", METADATA[0], f"meta:70:0:default.audio.sink:{value}", "volume-port")
    assert out.splitlines()[-1] == f"volume-port={port}"


def test_default_sink_ignores_other_entries(audio):
    out = audio("setup", *METADATA, "meta:70:0:default.audio.source:{\"name\":\"hal_output\"}",
                "meta:70:42:default.audio.sink:-", "volume-port", "bt",
                # PipeWire clears all of the core's entries: the default is gone
                "meta:70:0:-:-", "volume-port", "bt")
    assert lines_of(out, "volume-port") == ["volume-port=none", "volume-port=hal_output"]
    assert [s["default"] for s in bt(out)] == ["bluez_output.AA.1", ""]


def test_default_metadata_goes_away(audio):
    out = audio("setup", *METADATA, "remove:70", "bt", *METADATA)
    assert "proxy-destroy 70 Metadata" in out
    assert bt(out)[0]["default"] == ""
    assert out.count("bind 70 Metadata") == 2


# ---- Hands-free ----

def test_call_on_the_headset(audio):
    out = audio(*HEADSET, *TO_SCO, "bt", "call:0:0", "idle", "timeouts", "fire:1500", "bt")
    lines = out.splitlines()
    # The headset to hands-free (not remembered), the hold on its hands-free sink, then the HAL:
    # the codec first, then both streams opened anew on SCO
    assert "audio-hal-bridge: bluetooth hands-free on for bluez_output.AA.1 (call)" in lines
    assert "set-profile 50 Profile index=2 save=0" in lines
    assert ("stream-new audio-hal-bridge.sco-hold media=audio-hal-bridge SCO hold: media.type=Audio "
            "media.category=Playback node.name=audio-hal-bridge.sco-hold target.object=bluez_output.AA.1 "
            "media.role=Hold node.dont-move=true") in lines
    assert ("stream-connect audio-hal-bridge.sco-hold output target=-1 flags=autoconnect,map-buffers, "
            "format=S16LE/16000/1/MONO") in lines
    i = lines.index("timeout-remove 8000")
    assert lines[i + 1:i + 11] == ["close-out 0x2", f"close-in {MIC:#x}", "hal bt_wbs=on", "hal BT_SCO=on",
                                   "open-out 0x10 handle=1 flags=0x2 rate=48000 mask=0x3 format=0x1",
                                   f"open-in {SCO_MIC:#x} handle=2 mask=0xc rate=48000 source=1",
                                   f"open-in {SCO_MIC:#x} handle=2 mask=0x10 rate=48000 source=1",
                                   "audio-hal-bridge: bluetooth sco on (wide band), input 1 channels",
                                   "out routing=16", f"in routing={SCO_MIC}"]
    states = bt(out)
    assert states[0]["sco_state"] == "2" and states[0]["codec"] == "msbc" and states[0]["sco"] == "1"
    # After the call hands-free stays a moment (apps reopen), then everything goes back
    assert "timeout pending 1500" in lines
    i = lines.index("audio-hal-bridge: bluetooth hands-free off: no call or recording any more")
    assert lines[i + 1:i + 4] == ["close-out 0x10", f"close-in {SCO_MIC:#x}", "hal BT_SCO=off"]
    assert after_all(lines, i, ["audio-hal-bridge: bluetooth sco off, input 2 channels", "out routing=2",
                                "stream-destroy audio-hal-bridge.sco-hold", "set-profile 50 Profile index=1 save=0"])
    assert states[1]["sco_state"] == "0" and states[1]["sco"] == "0" and states[1]["hold"] == "0"


def after_all(lines, start, wanted):
    """wanted come in this order after lines[start]."""
    at = start
    for w in wanted:
        at = lines.index(w, at + 1)
    return True


def test_call_back_within_the_release(audio):
    out = audio(*HEADSET, *TO_SCO, "call:0:0", "idle", "call:0:0", "idle", "call:1:0", "idle", "timeouts", "bt")
    # One release only, and the new call keeps hands-free
    assert out.count("timeout-add 1500") == 1 and "timeout-remove 1500" in out
    assert "timeout pending" not in out and bt(out)[-1]["sco_state"] == "2"


def test_codec_changes_while_on_sco(audio):
    out = audio(*HEADSET, *TO_SCO, sink_info("headset-head-unit", "cvsd"), "idle",
                sink_info("headset-head-unit", "lc3_swb"), "idle", sink_info("a2dp-sink", "sbc"), "idle", "bt")
    # The link came back with another codec: the HAL is told, streams reopened (narrow band);
    # codecs it has no word for and A2DP leave it as it is
    assert "audio-hal-bridge: bluetooth sco on (narrow band), input 1 channels" in out
    assert out.count("hal BT_SCO=on") == 2 and "hal bt_wbs=off" in out
    assert bt(out)[-1]["codec"] == "cvsd"


def test_headset_goes_away_on_sco(audio):
    out = audio(*HEADSET, *TO_SCO, "remove:50", "idle", "bt")
    assert "audio-hal-bridge: bluetooth hands-free off: the headset went away" in out
    assert "hal BT_SCO=off" in out
    # The device is gone: no profile to restore
    assert out.count("set-profile") == 1 and bt(out)[-1]["sco_state"] == "0"


def test_headset_goes_away_during_the_release(audio):
    out = audio(*HEADSET, *TO_SCO, "call:0:0", "idle", "remove:50", "idle", "timeouts", "bt")
    assert "audio-hal-bridge: bluetooth hands-free off: the headset went away" in out
    assert "timeout-remove 1500" in out and "timeout pending" not in out


def test_headset_already_in_hands_free(audio):
    # A car that connects with hands-free only: no profile switch, nothing to restore
    out = audio(*PRE, "setup", DEVICE, "dev-param:50:EnumProfile:2:headset-head-unit", "dev-param:50:Profile:2:headset-head-unit",
                SINK, sink_info("headset-head-unit", "cvsd"), *METADATA, "idle", "call:1:0", "idle",
                "state:audio-hal-bridge.sco-hold:streaming", "idle", "bt", "call:0:0", "idle", "fire:1500", "bt")
    assert "set-profile" not in out
    assert "audio-hal-bridge: bluetooth sco on (narrow band), input 1 channels" in out
    states = bt(out)
    assert states[0]["restore"] == "-1" and states[1]["sco_state"] == "0"


def test_profile_unknown_restores_a2dp(audio):
    out = audio(*PRE, "setup", DEVICE, *PROFILES[:3], SINK, sink_info(), *METADATA, "idle", "call:1:0", "idle", "bt")
    assert bt(out)[-1]["restore"] == "1"


def test_headset_that_does_not_switch(audio):
    out = audio(*HEADSET, "call:1:0", "idle", "fire:8000", "idle", "bt", sink_info("headset-head-unit", "msbc"),
                "idle", "bt", "call:0:0", "idle", "call:1:0", "idle", "bt")
    assert "audio-hal-bridge: bluetooth hands-free off: the headset did not take hands-free" in out
    states = bt(out)
    # No new attempt while the call lasts; the next call tries again
    assert states[0]["blocked"] == "1" and states[1]["sco_state"] == "0"
    assert states[2]["sco_state"] == "1" and states[2]["blocked"] == "0"
    assert out.count("set-profile 50 Profile index=2 save=0") == 2


def test_timeout_after_the_switch_does_nothing(audio):
    out = audio(*HEADSET, *TO_SCO, "fire:8000", "bt")
    # The switch took: its timeout was removed, nothing left to fire
    assert "no timeout 8000 pending" in out and bt(out)[-1]["sco_state"] == "2"


@pytest.mark.parametrize("cmds,why", [
    (["call:0:0"], "not needed any more"),
    (["remove:50"], "the headset went away"),
])
def test_switch_given_up(audio, cmds, why):
    out = audio(*HEADSET, "call:1:0", "idle", *cmds, "idle", "bt")
    assert f"audio-hal-bridge: bluetooth hands-free off: {why}" in out
    assert "timeout-remove 8000" in out and bt(out)[-1]["sco_state"] == "0"
    # Never on SCO: the HAL streams stay
    assert "BT_SCO" not in out


def test_switch_survives_the_nodes_being_replaced(audio):
    # While the profile changes the default sink has no device for a moment: the call waits
    out = audio(*HEADSET, "call:1:0", "idle", "remove:60", "idle", "bt", SINK.replace("60", "61"),
                sink_info("headset-head-unit", "msbc", node=61), "idle", "state:audio-hal-bridge.sco-hold:streaming",
                "idle", "bt")
    states = bt(out)
    assert states[0]["sco_state"] == "1" and states[1]["sco_state"] == "2"
    assert "hands-free off" not in out


@pytest.mark.parametrize("profiles,stop", [
    (PROFILES, False),                        # narrow band is understood by all: to cvsd
    (PROFILES[:2] + PROFILES[3:], True),      # no cvsd profile: given up
])
def test_codec_the_hal_cannot_code(audio, profiles, stop):
    out = audio(*PRE, "setup", DEVICE, *profiles, SINK, sink_info(), *METADATA, "idle", "call:1:0", "idle",
                sink_info("headset-head-unit", "lc3_swb"), "idle", "state:audio-hal-bridge.sco-hold:streaming", "idle",
                "bt")
    assert ("set-profile 50 Profile index=3 save=0" in out) != stop
    assert ("audio-hal-bridge: bluetooth hands-free off: no codec the HAL can code" in out) == stop
    assert "BT_SCO" not in out


def test_already_cvsd_with_an_unknown_codec(audio):
    out = audio(*PRE, "setup", DEVICE, *PROFILES, SINK, sink_info(), *METADATA, "idle", "call:1:0", "idle",
                "dev-param:50:Profile:3:headset-head-unit-cvsd", sink_info("headset-head-unit-cvsd", "lc3_swb"),
                "idle", "state:audio-hal-bridge.sco-hold:streaming", "idle")
    assert "audio-hal-bridge: bluetooth hands-free off: no codec the HAL can code" in out


@pytest.mark.parametrize("cmds", [
    ["call:1:1"],                                                         # the call is on the speaker
    ['meta:70:0:default.audio.sink:{"name":"hal_output"}', "call:1:0"],   # the call is on the phone
])
def test_call_not_on_the_headset(audio, cmds):
    out = audio(*HEADSET, *cmds, "idle", "bt")
    assert "set-profile" not in out and bt(out)[-1]["sco_state"] == "0"


def test_recording_from_the_headset(audio):
    out = audio(*HEADSET, STREAM, "link:90:hal_headset_input:80", "idle", "bt", "remove:90", "idle", "bt")
    assert "audio-hal-bridge: bluetooth hands-free on for bluez_output.AA.1 (recording)" in out
    states = bt(out)
    assert states[0]["links"] == "1" and states[0]["sco_state"] == "1"
    assert states[1]["links"] == "0" and states[1]["sco_state"] == "0"


def test_loopback_on_the_headset_source_is_no_recording(audio):
    # WirePlumber's own headset loopback links to it in hands-free: not an app recording
    out = audio(*HEADSET, "global:80:Node:media.class=Stream/Input/Audio;client.id=5;node.link-group=x",
                "link:90:hal_headset_input:80", "idle", "bt")
    assert bt(out)[-1]["links"] == "1" and bt(out)[-1]["sco_state"] == "0"


def test_headset_links_table_full(audio):
    out = audio(*HEADSET, "links:400:65:hal_headset_input:100", "bt", "remove:400", "bt")
    assert [s["links"] for s in bt(out)] == ["64", "63"]


def test_hold_stream_sends_silence(audio):
    out = audio(*HEADSET, "call:1:0", "idle", sink_info("headset-head-unit", ""), "idle",
                "stream:audio-hal-bridge.sco-hold:requested:160", "process:audio-hal-bridge.sco-hold",
                "stream:audio-hal-bridge.sco-hold:requested:0", "process:audio-hal-bridge.sco-hold",
                "stream:audio-hal-bridge.sco-hold:nodata:1", "process:audio-hal-bridge.sco-hold",
                "stream:audio-hal-bridge.sco-hold:nobuffer:1", "process:audio-hal-bridge.sco-hold")
    # 160 frames of mono S16 asked: 320 bytes of zeros; nothing asked: the whole buffer
    assert lines_of(out, "deliver audio-hal-bridge.sco-hold") == [
        "deliver audio-hal-bridge.sco-hold 320 offset=0 first=0 last=0 stride=2",
        "deliver audio-hal-bridge.sco-hold 32768 offset=0 first=0 last=0 stride=2",
        "deliver audio-hal-bridge.sco-hold no data"]


def test_hold_stream_that_cannot_be_made(audio):
    out = audio("fail:stream_new:audio-hal-bridge.sco-hold", *HEADSET, "call:1:0", "idle",
                sink_info("headset-head-unit", "msbc"), "idle", "bt")
    # No hold, no link: the switch waits (and its timeout ends it)
    assert bt(out)[-1]["hold"] == "0" and bt(out)[-1]["sco_state"] == "1"


# ---- The HAL streams on SCO ----

def test_hal_streams_cannot_go_to_sco(audio):
    out = audio("setup", "fail:open_out:0x10", "hal-sco:1:1", "bt")
    assert "audio-hal-bridge: no HAL streams on Bluetooth SCO, staying on the phone" in out
    assert "audio-hal-bridge: bluetooth sco off, input 2 channels" in out
    assert bt(out)[-1]["sco"] == "0"


def test_hal_input_cannot_go_to_sco(audio):
    out = audio("setup", f"fail:open_in:{SCO_MIC:#x}:0xc,open_in:{SCO_MIC:#x}:0x10", "hal-sco:1:0")
    lines = out.splitlines()
    i = lines.index("audio-hal-bridge: no HAL streams on Bluetooth SCO, staying on the phone")
    # The SCO output already open is closed again, the phone's streams come back
    assert lines[i + 1:i + 3] == ["close-out 0x10", "hal BT_SCO=off"]


def test_hal_streams_lost(audio):
    r = audio("setup", "fail:open_out:0x2", "hal-sco:0:0", check=False)
    # Without streams the bridge cannot go on: it ends, the service restarts it
    assert r.returncode == 134
    assert r.stdout.splitlines()[-2:] == ["audio-hal-bridge: cannot reopen the HAL streams", "aborted"]


def test_input_reopened_after_a_call(audio):
    out = audio("setup", "reopen-input", "sco:1", f"fail:open_in:{SCO_MIC:#x}:0xc", "reopen-input")
    out = out.split("get-registry\n", 1)[1]
    assert lines_of(out, "close-in", "open-in", "in-channels") == [
        f"close-in {MIC:#x}", f"open-in {MIC:#x} handle=2 mask=0xc rate=48000 source=1", "in-channels=2",
        f"close-in {MIC:#x}", f"open-in {SCO_MIC:#x} handle=2 mask=0xc rate=48000 source=1",
        f"open-in {SCO_MIC:#x} handle=2 mask=0x10 rate=48000 source=1", "in-channels=1"]


def test_input_lost_after_a_call(audio):
    r = audio("setup", f"fail:open_in:{MIC:#x}:0xc,open_in:{MIC:#x}:0x10", "reopen-input", check=False)
    assert r.returncode == 134
    assert r.stdout.splitlines()[-2:] == ["audio-hal-bridge: cannot reopen the HAL input after the call", "aborted"]


# ---- Ringing in-band: a ringing call takes hands-free on headsets and headphones ----

WEARABLE = "dev-props:50:device.form-factor=headset"
# feedbackd's ringtone as Arch builds it: the role "event", the event id tells it apart
RINGTONE = ["global:85:Node:media.class=Stream/Output/Audio;client.id=7",
            "node-info:85:application.process.binary=feedbackd;media.role=event;media.name=phone-incoming-call"]
RINGING = [WEARABLE, *RINGTONE, "idle"]
# The ringtone reaches the headset over SCO
RING_TO_SCO = [*RINGING, sink_info("headset-head-unit", ""), "idle", sink_info("headset-head-unit", "msbc"), "idle",
               "state:audio-hal-bridge.sco-hold:streaming", "idle"]


@pytest.mark.parametrize("ff", ["headset", "headphone"])
def test_ringing_goes_to_the_headset_in_hands_free(audio, ff):
    out = audio(*HEADSET, WEARABLE.replace("headset", ff), *RING_TO_SCO[1:], "bt",
                # The call stops ringing: a moment later ringing is over, then hands-free
                "remove:85", "idle", "timeouts", "fire:2000", "idle", "fire:1500", "bt")
    lines = out.splitlines()
    assert "audio-hal-bridge: bluetooth hands-free on for bluez_output.AA.1 (ringing)" in lines
    assert "set-profile 50 Profile index=2 save=0" in lines
    assert "hal BT_SCO=on" in lines and "out routing=16" in lines
    states = bt(out)
    assert states[0]["sco_state"] == "2" and states[0]["ringing"] == "1"
    assert "timeout pending 2000" in lines
    assert after_all(lines, lines.index("timeout pending 2000"),
                     ["audio-hal-bridge: bluetooth hands-free off: no call or recording any more", "hal BT_SCO=off",
                      "set-profile 50 Profile index=1 save=0"])
    assert states[1]["sco_state"] == "0" and states[1]["ringing"] == "0"


def test_ringtone_known_by_its_event_id(audio):
    out = audio(*HEADSET, WEARABLE, RINGTONE[0],
                "node-info:85:application.process.binary=feedbackd;event.id=phone-incoming-call", "idle", "bt",
                "node-info:85:application.process.binary=feedbackd", "idle", "bt")
    # Learned once, it stays a ringtone
    assert [s["ringing"] for s in bt(out)] == ["1", "1"]
    assert "(ringing)" in out


@pytest.mark.parametrize("cmds", [
    [WEARABLE.replace("headset", "car"), *RINGTONE],          # a car kit rings itself
    [WEARABLE.replace("headset", "hands-free"), *RINGTONE],
    ["dev-info:50", *RINGTONE],                                 # no form factor known
    [WEARABLE, RINGTONE[0], RINGTONE[1].replace("phone-incoming-call", "message-new-instant")],
    [WEARABLE, RINGTONE[0], RINGTONE[1].replace("feedbackd", "gnome-control-center")],  # a preview
    [WEARABLE, RINGTONE[0].replace("Output", "Input"), RINGTONE[1]],                   # not playback
    [WEARABLE, *RINGTONE, 'meta:70:0:default.audio.sink:{"name":"hal_output"}'],         # rings on the phone
    [WEARABLE, "call:1:1", *RINGTONE],                          # a call runs (on the speaker): it waits
])
def test_no_hands_free_for_ringing(audio, cmds):
    out = audio(*HEADSET, *cmds, "idle", "bt")
    assert "set-profile" not in out and bt(out)[-1]["sco_state"] == "0"


def test_the_next_ring_keeps_hands_free(audio):
    # feedbackd plays the ringtone anew for every ring: a new stream within the gap
    out = audio(*HEADSET, *RING_TO_SCO, "remove:85", "idle", "global:86:Node:media.class=Stream/Output/Audio;client.id=7",
                RINGTONE[1].replace("85", "86"), "idle", "timeouts", "bt")
    assert "timeout-add 2000" in out and "timeout-remove 2000" in out
    assert "timeout pending" not in out and "hands-free off" not in out
    assert bt(out)[-1]["sco_state"] == "2" and bt(out)[-1]["ringing"] == "1"


def test_answered_while_ringing(audio):
    out = audio(*HEADSET, *RING_TO_SCO, "call:1:0", "idle", "remove:85", "idle", "fire:2000", "idle", "bt")
    # The call goes on over the same link: nothing switches back
    assert "hands-free off" not in out and out.count("set-profile") == 1
    assert bt(out)[-1]["sco_state"] == "2" and bt(out)[-1]["ringing"] == "0"


def test_ringing_survives_the_nodes_being_replaced(audio):
    out = audio(*HEADSET, *RINGING, "remove:60", "idle", "bt", SINK.replace("60", "61"),
                sink_info("headset-head-unit", "msbc", node=61), "idle", "state:audio-hal-bridge.sco-hold:streaming",
                "idle", "bt")
    states = bt(out)
    assert states[0]["sco_state"] == "1" and states[1]["sco_state"] == "2"
    assert "hands-free off" not in out


def test_ringing_stops_during_the_switch(audio):
    out = audio(*HEADSET, *RINGING, "remove:85", "idle", "fire:2000", "idle", "bt")
    assert "audio-hal-bridge: bluetooth hands-free off: not needed any more" in out
    assert "BT_SCO" not in out and bt(out)[-1]["sco_state"] == "0"
