# audio-hal-bridge as a whole: its main with the stand-in HAL and PipeWire (c/dhw_audio_harness.c)
# on a D-Bus session bus of the test's own (dbus-daemon with its socket in tmp, nothing
# activatable). Startup (HAL, nodes, registry, threads, jack, FM), callaudiod's API as GNOME
# Calls uses it (modes, speaker, microphone, properties and their signal), and the shutdown on
# SIGTERM, which hands a headset in hands-free back. The script runs in the stand-in PipeWire
# thread; "barrier" waits until the main loop has done what was queued.

import pytest

from dhw_audio_helpers import MIC, Bus, audio, jack_device, SW_HEADPHONE_INSERT  # noqa: F401 (fixture)


@pytest.fixture(scope="module")
def bus(tmp_path_factory):
    b = Bus(tmp_path_factory.mktemp("bus"))
    yield b.address
    b.stop()


def lines_of(out, *prefixes):
    return [line for line in out.splitlines() if line.startswith(prefixes)]


def test_startup_and_shutdown(audio, bus):
    r = audio.main(["dbus-wait", "sigterm"], bus=bus)
    lines = r.stdout.splitlines()
    assert r.returncode == 0 and lines[-1] == "main=0"
    # The HAL first (normal mode, speaker, both microphones), the canceller, then PipeWire
    order = ["hw-module audio.primary", "set-mode=0", "open-out 0x2 handle=1 flags=0x2 rate=48000 mask=0x3 format=0x1",
             f"open-in {MIC:#x} handle=2 mask=0xc rate=48000 source=1", "aec-new rate=48000 render=2 capture=1",
             "pw-init", "loop-start", "context-new", "get-registry", "dbus ready"]
    at = [lines.index(o) for o in order]
    assert at == sorted(at)
    assert [line.split(" ")[1] for line in lines_of(r.stdout, "stream-connect")] == ["hal_output", "hal_earpiece",
                                                                                     "hal_input"]
    # Both threads went to standby (nothing streams), no FM tuner, no jack
    assert "out-standby" in lines and "in-standby" in lines and "hal_fm_input" not in r.stdout
    # Shutdown: the registry, the nodes, the HAL, in this order
    end = ["proxy-destroy 0 Registry", "core-disconnect", "context-destroy", "stream-destroy hal_output",
           "stream-destroy hal_earpiece", "stream-destroy hal_input", "loop-stop", "loop-destroy", "close-out 0x2",
           f"close-in {MIC:#x}", "hal-close", "aec-free", "pw-deinit"]
    at = [lines.index(e) for e in end]
    assert at == sorted(at) and at[0] > lines.index("dbus ready")


def test_call_audio_api(audio, bus):
    r = audio.main(["dbus-wait",
                    "dbus-get:AudioMode", "dbus-get:SpeakerState", "dbus-get:MicState", "dbus-get:Volume",
                    "dbus:SelectMode:(uint32 1,)", "barrier", "dbus-signals:1",
                    "dbus:EnableSpeaker:(true,)", "dbus:MuteMic:(true,)",
                    "dbus-get:SpeakerState", "dbus-get:MicState",
                    "dbus:SelectMode:(uint32 0,)", "barrier",
                    "dbus:SelectMode:(uint32 5,)", "dbus-signals:4", "sigterm"], bus=bus)
    out = r.stdout
    lines = out.splitlines()
    replies = lines_of(out, "reply", "error")
    assert replies == [
        "reply AudioMode (<uint32 0>,)", "reply SpeakerState (<uint32 1>,)", "reply MicState (<uint32 1>,)",
        "error Volume GDBus.Error:org.freedesktop.DBus.Error.InvalidArgs: No such property “Volume”",
        "reply SelectMode (true,)", "reply EnableSpeaker (true,)", "reply MuteMic (true,)",
        "reply SpeakerState (<uint32 1>,)", "reply MicState (<uint32 0>,)", "reply SelectMode (true,)",
        "error SelectMode GDBus.Error:org.freedesktop.DBus.Error.InvalidArgs: Unknown mode 5"]
    # Call mode: the HAL in call mode, the earpiece, the call volume on the speaker's slider
    i = lines.index("set-mode=2")
    assert lines[i + 1:i + 3] == ["out routing=1", f"in routing={MIC}"]
    assert "audio-hal-bridge: call mode" in lines
    assert "control hal_output channelVolumes=0.630,0.630" in lines and "voice-volume=0.857" in lines
    # Speaker, microphone muted
    assert lines.index("out routing=2") > i and "mic-mute=1" in lines
    # Back to normal: the input opened anew (the HAL leaves it suspended after a call)
    j = lines.index("set-mode=0", i)
    assert lines[j + 1:j + 3] == [f"close-in {MIC:#x}", f"open-in {MIC:#x} handle=2 mask=0xc rate=48000 source=1"]
    assert "audio-hal-bridge: normal mode" in lines
    assert "control hal_output channelVolumes=1.000,1.000" in lines
    # Every change is signalled with all three properties
    signals = lines_of(out, "signal ")
    prefix = ("signal org.freedesktop.DBus.Properties.PropertiesChanged /org/mobian_project/CallAudio "
              "('org.mobian_project.CallAudio', ")
    assert signals == [
        prefix + "{'AudioMode': <uint32 1>, 'SpeakerState': <uint32 0>, 'MicState': <uint32 1>}, [])",
        prefix + "{'AudioMode': <uint32 1>, 'SpeakerState': <uint32 1>, 'MicState': <uint32 1>}, [])",
        prefix + "{'AudioMode': <uint32 1>, 'SpeakerState': <uint32 1>, 'MicState': <uint32 0>}, [])",
        prefix + "{'AudioMode': <uint32 0>, 'SpeakerState': <uint32 1>, 'MicState': <uint32 0>}, [])"]


def test_speaker_state_with_headphones(audio, bus):
    jack_device(audio.cwd, "Headset Jack", 0, states=[[SW_HEADPHONE_INSERT]])
    r = audio.main(["dbus-wait", "barrier", "dbus-get:SpeakerState", "sigterm"], bus=bus)
    # Outside calls SpeakerState says whether the speaker plays: not with headphones
    assert "reply SpeakerState (<uint32 0>,)" in r.stdout
    # The headphones' sink came with the plug and goes at the end
    assert "stream-connect hal_headphones" in r.stdout and "stream-destroy hal_headphones" in r.stdout


def test_fm_tuner(audio, bus):
    (audio.cwd / "fm").write_text("")
    r = audio.main(["dbus-wait", "sigterm"], bus=bus)
    assert "stream-connect hal_fm_input output" in r.stdout and "stream-destroy hal_fm_input" in r.stdout


def test_shutdown_hands_the_headset_back(audio, bus):
    r = audio.main(["global:50:Device:device.api=bluez5;media.class=Audio/Device",
                    "dev-param:50:EnumProfile:1:a2dp-sink", "dev-param:50:EnumProfile:2:headset-head-unit",
                    "dev-param:50:Profile:1:a2dp-sink",
                    "global:60:Node:node.name=bluez_output.AA.1;media.class=Audio/Sink",
                    "node-info:60:device.id=50;node.description=Headset X;api.bluez5.profile=a2dp-sink",
                    "global:70:Metadata:metadata.name=default",
                    'meta:70:0:default.audio.sink:{"name":"bluez_output.AA.1"}',
                    "dbus-wait", "dbus:SelectMode:(uint32 1,)", "barrier", "sigterm"], bus=bus)
    lines = r.stdout.splitlines()
    assert "set-profile 50 Profile index=2 save=0" in lines
    i = lines.index("audio-hal-bridge: bluetooth hands-free off: stopping")
    assert "set-profile 50 Profile index=1 save=0" in lines[i:]
    assert "stream-destroy hal_headset_input" in lines[i:]


@pytest.mark.parametrize("fail,message", [
    ("hw_module", "audio-hal-bridge: no primary audio HAL"),
    ("loop_start", "audio-hal-bridge: cannot connect to PipeWire"),
    ("stream_connect:hal_input", "audio-hal-bridge: cannot connect to PipeWire"),
])
def test_startup_fails(audio, fail, message):
    r = audio.main([], f"fail:{fail}", check=False)
    assert r.returncode == 1 and r.stdout.splitlines()[-2:] == [message, "main=1"]


def test_startup_without_extras(audio, bus):
    (audio.cwd / "fm").write_text("")
    r = audio.main(["dbus-wait", "sigterm"], "fail:aec_new,context,stream_connect:hal_fm_input", bus=bus)
    out = r.stdout
    # No canceller, no registry, no FM source: the bridge runs without them
    assert "audio-hal-bridge: no echo canceller" in out
    assert "audio-hal-bridge: no registry, no echo cancellation" in out
    assert "audio-hal-bridge: no FM source" in out
    assert r.returncode == 0 and "aec-free" not in out and "core-disconnect" not in out


def test_property_getter(audio):
    out = audio("route:0:1:0:0:0:0:0", "property:SpeakerState", "route:0:1:0:1:1:0:0", "property:SpeakerState",
                "property:AudioMode", "property:Volume")
    # Outside a call: not with headphones; in a call: as chosen
    assert lines_of(out, "reply", "error") == ["reply SpeakerState 0", "reply SpeakerState 1",
                                               "reply AudioMode 1", "error Volume Unknown property Volume"]
