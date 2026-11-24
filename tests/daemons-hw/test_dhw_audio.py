# audio-hal-bridge: where the HAL streams are routed (earpiece, speaker, wired headset,
# Bluetooth SCO; in and out of calls), the speakerphone switch for the echo canceller, the ring
# buffers between PipeWire and the HAL, the 10 ms reference queue of the echo canceller, the call
# volume (steps, role volumes, the keys during a call) and its state file; opening the HAL and its
# streams, the nodes the user sees, their events (state, volume, process), the earpiece
# amplifier, the headset jack. The Android audio HAL, libpipewire and ALSA are stand-ins
# (c/dhw_audio_harness.c, fakes/hardware/), WebRTC's canceller is replaced by a recorder.

import pytest

from dhw_audio_helpers import (EARPIECE, HEADPHONE, HEADSET, HEADSET_MIC, MIC, S_EARPIECE, S_PHONES,
                               S_SPEAKER, SCO, SCO_MIC, SPEAKER, SW_HEADPHONE_INSERT, SW_LINEOUT_INSERT,
                               SW_MICROPHONE_INSERT, audio, jack_device, kv)  # noqa: F401 (fixture)


@pytest.mark.parametrize("state,out,inp", [
    # sco headphones mic call call_speaker playing
    ("0:0:0:0:0:0", SPEAKER, MIC),            # nothing plays: speaker
    (f"0:0:0:0:0:{S_SPEAKER}", SPEAKER, MIC),  # media: speaker
    (f"0:0:0:0:0:{S_EARPIECE}", EARPIECE, MIC),  # only the earpiece sink plays
    # The earpiece sink next to another one: speaker, so no sound hides in the earpiece
    (f"0:0:0:0:0:{S_EARPIECE | S_SPEAKER}", SPEAKER, MIC),
    (f"0:0:0:0:0:{S_EARPIECE | S_PHONES}", SPEAKER, MIC),
    (f"0:0:0:0:0:{S_PHONES}", SPEAKER, MIC),  # headphones sink without a plug: speaker
    (f"0:1:0:0:0:{S_SPEAKER}", HEADPHONE, MIC),  # the plug wins over the speaker sink
    (f"0:1:1:0:0:{S_EARPIECE}", HEADSET, HEADSET_MIC),  # and over the earpiece sink
    ("0:0:0:1:0:0", EARPIECE, MIC),           # call: earpiece
    ("0:0:0:1:1:0", SPEAKER, MIC),            # call on speaker
    (f"0:0:0:1:1:{S_EARPIECE}", SPEAKER, MIC),  # in a call the earpiece sink does not decide
    (f"0:0:0:1:0:{S_SPEAKER}", EARPIECE, MIC),  # nor the speaker sink
    ("0:1:0:0:0:0", HEADPHONE, MIC),          # headphones without microphone: phone's mic
    ("0:1:1:0:0:0", HEADSET, HEADSET_MIC),    # headset with microphone
    ("0:1:1:1:1:0", HEADSET, HEADSET_MIC),    # a call stays on the plug even "on speaker"
    ("1:1:1:1:1:0", SCO, SCO_MIC),            # Bluetooth hands-free wins over everything
])
def test_routing(audio, state, out, inp):
    r = audio(f"route:{state}:0")
    assert f"out routing={out}" in r and f"in routing={inp}" in r
    assert kv(r)["routed"].startswith(f"{out:#x} speaker={int(out == SPEAKER)}")
    mode = "call" if state.split(":")[3] == "1" else "normal"
    assert f"log mode {mode}, output {out:#x}, input {inp:#x}" in r


@pytest.mark.parametrize("state,aec,on", [
    ("0:0:0:0:0:0", 1, 1),   # app conversation over the speaker
    ("0:0:0:0:0:0", 0, 0),   # no voice stream recording
    ("0:0:0:1:1:0", 1, 0),   # a phone call is not an app conversation
    (f"0:0:0:0:0:{S_EARPIECE}", 1, 0),   # earpiece
    ("0:1:0:0:0:0", 1, 0),   # headphones
])
def test_speakerphone(audio, state, aec, on):
    assert kv(audio(f"route:{state}:{aec}"))["routed"].endswith(f"speakerphone={on}")


def test_speakerphone_is_logged_when_it_changes(audio):
    out = audio("route:0:0:0:0:0:0:1", "route:0:0:0:0:0:0:1", "route:0:1:0:0:0:0:1")
    assert out.count("log speakerphone on") == 1 and out.count("log speakerphone off") == 1


def ring(out):
    return [line.split("=", 1)[1] for line in out.splitlines() if line.startswith("ring ")]


def test_ring_buffer(audio):
    out = audio("ring:1000,600:500,0:70000,0:64637,0:64636,0:0,65536:0,10")
    assert ring(out) == [
        "fill 400 read 600 in-order 1",
        "fill 900 read 0 in-order 1",
        "fill 900 read 0 in-order 1",     # more than the ring holds: dropped whole
        "fill 900 read 0 in-order 1",     # more than the free space: dropped whole
        "fill 65536 read 0 in-order 1",   # exactly full
        "fill 0 read 65536 in-order 1",   # across the end of the buffer, in order
        "fill 0 read 0 in-order 1",       # nothing to read
    ]


def test_echo_reference_blocks(audio):
    # 10 ms blocks (480 frames): partial blocks wait, each microphone block takes one reference
    lines = audio.lines("play:300:1", "play:180:1", "play:480:2", "record:200", "record:400", "record:960")
    assert lines[0] == "queued=0 fill=300"
    assert lines[1] == "queued=1 fill=0"
    assert lines[2] == "queued=2 fill=0"
    assert lines[3] == "cancelled bytes=0 queued=2 pending=200 refs="
    # 600 frames recorded: one block done (960 bytes of mono S16), 120 wait
    assert lines[4] == "cancelled bytes=960 queued=1 pending=120 refs=1,"
    # Two more blocks: the second reference, then silence when none is queued
    assert lines[5] == "cancelled bytes=1920 queued=0 pending=120 refs=2,0,"


def test_echo_reference_queue_drops_the_oldest(audio):
    # 64 blocks fit (640 ms); the capture side stalled: the oldest go
    out = audio(*[f"play:480:{i}" for i in range(1, 71)], "record:480")
    assert out.splitlines()[-1] == "cancelled bytes=960 queued=63 pending=0 refs=7,"


def test_echo_reference_reset(audio):
    out = audio("play:480:5", "play:100:5", "ref-reset", "play:400:6", "record:480")
    # After the reset the half block is gone too: 400 frames are no block yet, silence
    assert out.splitlines()[-1] == "cancelled bytes=960 queued=0 pending=0 refs=0,"


@pytest.mark.parametrize("role,voice", [("Communication", 1), ("phone", 1), ("Music", 0), ("", 0)])
def test_voice_role(audio, role, voice):
    assert kv(audio(f"role:{role}"))[f"role {role}".strip()] == str(voice)


@pytest.mark.parametrize("profile,hfp", [("headset-head-unit", 1), ("headset-head-unit-cvsd", 1),
                                         ("a2dp-sink", 0), ("off", 0)])
def test_hfp_profile(audio, profile, hfp):
    assert kv(audio(f"hfp:{profile}"))[f"hfp {profile}"] == str(hfp)


def test_call_volume_file(audio):
    f = audio.cwd / "state/audio-hal-bridge/call-volume"
    assert kv(audio("load-volume"))["call-step"] == "6"        # default
    audio("save-volume:3")
    assert f.read_text() == "3\n"
    assert (f.parent.stat().st_mode & 0o777) == 0o700
    assert kv(audio("load-volume"))["call-step"] == "3"
    for bad in ("0\n", "8\n", "x\n"):
        f.write_text(bad)
        assert kv(audio("load-volume"))["call-step"] == "6"


def test_call_volume_file_not_writable(audio):
    (audio.cwd / "state").write_text("a file where the directory should be")
    out = audio("save-volume:3")
    assert f"audio-hal-bridge: cannot save the call volume in {audio.cwd}/state/audio-hal-bridge/call-volume" in out


@pytest.mark.parametrize("step,volume", [(1, 1 / 7), (6, 6 / 7), (7, 1.0)])
def test_call_volume_steps(audio, step, volume):
    out = audio(f"step-volume:{step}")
    assert float(kv(out)["voice-volume"]) == pytest.approx(volume, abs=1e-3)
    assert f"audio-hal-bridge: call volume {step}/7 (test)\n" in out


def test_call_volume_the_hal_refuses(audio):
    assert "audio-hal-bridge: call volume 6/7 (test) failed" in audio("fail:voice_volume", "step-volume:6")


@pytest.mark.parametrize("role,sink,start,volume", [
    (0.125, 0.512, 1, 0.5 * 0.8),     # sliders are cube roots of gains: 0.5 x 0.8
    (1.0, "-", 1, 1.0),               # headset: the call channel alone
    (0.0, 0.512, 1, 1 / 7),           # never below the first step
    (8.0, 1.0, 1, 1.0),
])
def test_role_call_volume(audio, role, sink, start, volume):
    out = audio(f"role-volume:{role}:{sink}:{start}")
    assert float(kv(out)["voice-volume"]) == pytest.approx(volume, abs=1e-3)


def test_role_call_volume_ignores_tiny_changes(audio):
    out = audio("role-volume:0.125:0.512:1", "role-volume:0.1251:0.512:0", "role-volume:0.2:0.512:0")
    # The second differs by less than 0.005: not handed to the HAL again
    assert out.count("voice-volume=") == 2
    assert "= output speaker x call channel 0.50 (call start)" in out
    assert "(change)" in out


def test_role_call_volume_the_hal_refuses(audio):
    out = audio("fail:voice_volume", "role-volume:1:-:1")
    assert "audio-hal-bridge: call volume 1.00 = output headset x call channel 1.00 (call start) failed" in out


# ---- The HAL ----

def test_open_hal(audio):
    lines = audio.lines("open-hal")
    # Normal mode, the speaker's 48 kHz stereo output, both microphones as a stereo input
    assert lines[:5] == ["hw-module audio.primary", "hal-open audio_hw_if", "set-mode=0",
                         "open-out 0x2 handle=1 flags=0x2 rate=48000 mask=0x3 format=0x1",
                         f"open-in {MIC:#x} handle=2 mask=0xc rate=48000 source=1"]
    # One HAL transfer per node: the output's buffer, half the stereo input's for the mono
    # microphone (and the headset's source on the same reads); the FM input takes 1024 frames
    assert lines[-1] == "chunks out=16384 mic=2048 headset=2048 fm=4096 in-channels=2 has-fm=0 frame-sizes=4,2,4"


def test_open_hal_sees_the_fm_tuner(audio):
    (audio.cwd / "fm").write_text("")
    assert "has-fm=1" in audio("open-hal")


@pytest.mark.parametrize("fail", [f"in_mismatch:{MIC:#x}:0xc", f"open_in:{MIC:#x}:0xc",
                                  f"open_in_null:{MIC:#x}:0xc"])
def test_open_hal_mono_input(audio, fail):
    # No stereo input (refused, or offered otherwise): the lower microphone alone, mono
    lines = audio.lines(f"fail:{fail}", "open-hal")
    assert lines[-2:] == ["open-hal=0", "chunks out=16384 mic=4096 headset=4096 fm=4096 in-channels=1 has-fm=0 "
                          "frame-sizes=4,2,4"]
    assert f"open-in {MIC:#x} handle=2 mask=0x10 rate=48000 source=1" in lines
    # A stream offered at another rate is closed again
    assert (f"close-in {MIC:#x}" in lines) == fail.startswith("in_mismatch")


@pytest.mark.parametrize("out_buffer,in_buffer,chunks", [
    (0, 0, "out=4096 mic=2048"),            # the HAL names no size: 1024 frames
    (20000, 40000, "out=4096 mic=2048"),    # more than a transfer may be: 1024 frames
    (4098, 3002, "out=4096 mic=1500"),      # whole frames only
])
def test_open_hal_transfer_sizes(audio, out_buffer, in_buffer, chunks):
    out = audio(f"set:out_buffer:{out_buffer}", f"set:in_buffer:{in_buffer}", "open-hal")
    assert f"chunks {chunks} " in out


@pytest.mark.parametrize("fail,message", [
    ("hw_module", "no primary audio HAL"),
    ("hw_module_null", "no primary audio HAL"),
    ("hal_open", "cannot open the audio HAL"),
    ("hal_open_null", "cannot open the audio HAL"),
    ("init_check", "cannot open the audio HAL"),
    ("open_out:0x2", "no 48 kHz stereo output stream"),
    ("open_out_null:0x2", "no 48 kHz stereo output stream"),
    ("out_mismatch:0x2", "no 48 kHz stereo output stream"),
    (f"open_in:{MIC:#x}:0xc,open_in:{MIC:#x}:0x10", "no 48 kHz input stream"),
])
def test_open_hal_fails(audio, fail, message):
    lines = audio.lines(f"fail:{fail}", "open-hal")
    assert lines[-2:] == [f"audio-hal-bridge: {message}", "open-hal=-1"]
    # An output at another rate is not kept
    assert ("close-out 0x2" in lines) == (fail == "out_mismatch:0x2")


# ---- The nodes ----

def test_nodes(audio):
    out = audio("setup", "connect-port:hal_headphones", "connect-port:hal_headset_input",
                "connect-port:hal_fm_input")
    sinks = {"hal_output": ("speaker", "Speaker", "audio-speakers", "2000"),
             "hal_earpiece": ("earpiece", "Earpiece", "phone", "1000"),
             "hal_headphones": ("headphones", "Headphones", "audio-headphones", "2500")}
    for name, (media, description, icon, priority) in sinks.items():
        # One graph cycle per HAL transfer (16384 bytes of stereo S16: 4096 frames)
        assert (f"stream-new {name} media=audio-hal-bridge {media}: media.class=Audio/Sink node.name={name} "
                f"node.description={description} device.icon-name={icon} node.latency=4096/48000 "
                f"node.force-quantum=4096 node.force-rate=48000 priority.session={priority} "
                f"priority.driver=2000") in out
        assert f"stream-connect {name} input target=-1 flags=map-buffers,driver format=S16LE/48000/2/FL,FR" in out
    assert ("stream-new hal_input media=audio-hal-bridge microphone: media.class=Audio/Source node.name=hal_input "
            "node.description=Microphone device.icon-name=audio-input-microphone node.latency=1024/48000 "
            "node.force-quantum=1024 node.force-rate=48000 priority.session=2000 priority.driver=2000\n") in out
    assert "stream-connect hal_input output target=-1 flags=map-buffers,driver format=S16LE/48000/1/MONO" in out
    # Headphones and the headset's source are a device that just connected (new-device-default.lua)
    assert out.count("audio-hal-bridge.headset=true") == 2
    assert "priority.session=2500 priority.driver=2000 audio-hal-bridge.headset=true" in out
    assert "priority.session=1500 priority.driver=2000 audio-hal-bridge.headset=true" in out
    # The FM source: stereo, far below the microphones, hidden from the input choice
    assert ("stream-new hal_fm_input media=audio-hal-bridge fm radio: media.class=Audio/Source "
            "node.name=hal_fm_input node.description=FM Radio device.icon-name=audio-input-line "
            "node.latency=1024/48000 node.force-quantum=1024 node.force-rate=48000 priority.session=100 "
            "priority.driver=2000 plinius.hidden=true") in out
    assert "stream-connect hal_fm_input output target=-1 flags=map-buffers,driver format=S16LE/48000/2/FL,FR" in out


def test_node_that_cannot_be_made(audio):
    out = audio("setup", "fail:stream_new:hal_headphones,stream_connect:hal_fm_input",
                "connect-port:hal_headphones", "connect-port:hal_fm_input")
    assert "connect-port hal_headphones=-1" in out
    assert "connect-port hal_fm_input=-5" in out


def test_node_state(audio):
    out = audio("setup", "on:hal_output", "port:hal_output", "state:hal_output:error:no driver",
                "port:hal_output")
    assert "log hal_output: paused -> streaming" in out
    assert "port hal_output active=1" in out
    assert "log hal_output: paused -> error: no driver" in out
    assert out.splitlines()[-1].startswith("port hal_output active=0")


def test_node_volume(audio):
    out = audio.lines("setup", "control:hal_output:volume:0.5", "control:hal_output:channels:0.8,0.8",
                      "port:hal_output", "control:hal_output:volume:0", "port:hal_output",
                      # Not a volume, no values, not a sink: ignored
                      "control:hal_output:mute:1", "control:hal_output:volume:", "control:hal_input:volume:0.5",
                      "port:hal_output", "port:hal_input")
    ports = [line for line in out if line.startswith("port ")]
    # The echo reference undoes the volume (1 / 0.4), the call volume sees the gain
    assert ports[0] == "port hal_output active=0 cycles=0 period=16384 fill=0 unvolume=2.500 gain=0.400 channel=0.800"
    # Silent: nothing to undo
    assert ports[1].endswith("unvolume=0.000 gain=0.000 channel=0.800")
    assert ports[2] == ports[1]
    assert ports[3].endswith("unvolume=0.000 gain=0.000 channel=0.000")


def test_headphones_start_with_the_speakers_volume(audio):
    out = audio("setup", "control:hal_output:volume:0.5", "control:hal_output:channels:0.8,0.8",
                "headphones:1:0", "phones-update", "state:hal_headphones:paused", "state:hal_headphones:streaming",
                "state:hal_headphones:paused")
    # WirePlumber's stored volume for them is overwritten once, when they appear
    assert out.count("control hal_headphones volume=0.500 channelVolumes=0.800,0.800") == 1
    assert "log hal_headphones: the speaker's volume 0.50 x 0.80" in out


def test_headphones_sink_follows_the_plug(audio):
    out = audio("setup", "headphones:1:0", "phones-update", "phones-update", "on:hal_headphones",
                "headphones:0:0", "phones-update", "port:hal_headphones", "phones-update")
    assert out.count("stream-new hal_headphones") == 1
    assert out.count("stream-destroy hal_headphones") == 1
    assert "port hal_headphones active=0" in out


@pytest.mark.parametrize("fail,destroyed", [("stream_connect:hal_headphones", True),
                                            ("stream_new:hal_headphones", False)])
def test_headphones_sink_that_cannot_be_made(audio, fail, destroyed):
    out = audio("setup", f"fail:{fail}", "headphones:1:0", "phones-update")
    assert "audio-hal-bridge: no headphones sink" in out
    assert ("stream-destroy hal_headphones" in out) == destroyed


def test_sink_takes_the_graphs_data(audio):
    out = audio("setup", "stream:hal_output:value:7", "process:hal_output", "port:hal_output",
                # A chunk outside its buffer: nothing taken
                "stream:hal_output:offset:40000", "process:hal_output", "port:hal_output",
                "stream:hal_output:nobuffer:1", "process:hal_output", "port:hal_output",
                "stream:hal_output:nobuffer:0", "stream:hal_output:nodata:1", "process:hal_output",
                "port:hal_output")
    fills = [kv(line.replace(" ", "\n"))["fill"] for line in out.splitlines() if line.startswith("port ")]
    assert fills == ["4096", "4096", "4096", "4096"]


def test_source_gives_what_the_ring_holds(audio):
    out = audio.lines("setup", "fill:hal_input:3000:9", "process:hal_input",
                      # The graph asks for 500 frames: that much, and the period follows
                      "fill:hal_input:3000:8", "stream:hal_input:requested:500", "process:hal_input",
                      "port:hal_input", "stream:hal_input:requested:100000", "process:hal_input",
                      "port:hal_input", "process:hal_input",
                      "stream:hal_input:nodata:1", "process:hal_input", "stream:hal_input:nobuffer:1",
                      "process:hal_input")
    delivered = [line for line in out if line.startswith("deliver ")]
    assert delivered == ["deliver hal_input 3000 offset=0 first=9 last=9 stride=2",
                         "deliver hal_input 1000 offset=0 first=8 last=8 stride=2",
                         "deliver hal_input 2000 offset=0 first=8 last=8 stride=2",
                         "deliver hal_input 0 offset=0 first=0 last=0 stride=2",
                         "deliver hal_input no data"]
    ports = [line for line in out if line.startswith("port ")]
    assert "period=1000 " in ports[0]
    assert "period=16384 " in ports[1]   # never more than the largest transfer


def test_trigger_done_counts_cycles(audio):
    assert "cycles=2 " in audio("setup", "trigger-done:hal_input", "trigger-done:hal_input", "port:hal_input")


# ---- The call volume without role based volumes: the keys' sink shows it ----

def step_gain(step):
    return (step / 7) ** 3


def test_call_volume_on_the_keys_sink(audio):
    out = audio.lines(
        "setup", "control:hal_output:channels:0.5,0.5", "idle",
        "call:1:0", "idle",
        # The slider moved up a bit: one step at least (7), and the slider goes onto it
        f"control:hal_output:channels:{step_gain(6.4)}", "idle",
        # Down from 7 a bit: one step (6)
        f"control:hal_output:channels:{step_gain(6.9)}", "idle",
        # Hardly moved: no step, the slider stays
        f"control:hal_output:channels:{step_gain(6.02)}", "idle",
        # Further down: to the step it points at (3)
        f"control:hal_output:channels:{step_gain(3.1)}", "idle",
        "call:0:0", "idle", "call-volume")
    start = out.index("audio-hal-bridge: call volume 6/7 (call start)")
    assert out[start - 2:start] == [f"control hal_output channelVolumes={step_gain(6):.3f},{step_gain(6):.3f}",
                                    "voice-volume=0.857"]
    keys = [line for line in out[start + 1:] if line.startswith(("control", "voice-volume", "audio-hal-bridge: call"))]
    assert keys == [
        "control hal_output channelVolumes=1.000,1.000", "voice-volume=1.000", "audio-hal-bridge: call volume 7/7 (keys)",
        f"control hal_output channelVolumes={step_gain(6):.3f},{step_gain(6):.3f}", "voice-volume=0.857",
        "audio-hal-bridge: call volume 6/7 (keys)",
        f"control hal_output channelVolumes={step_gain(3):.3f},{step_gain(3):.3f}", "voice-volume=0.429",
        "audio-hal-bridge: call volume 3/7 (keys)",
        # The call ended: the media volume back
        "control hal_output channelVolumes=0.500,0.500"]
    assert out[-1].startswith("call-volume step=3 on=0 port=none")
    assert (audio.cwd / "state/audio-hal-bridge/call-volume").read_text() == "3\n"


def test_call_volume_slider_beyond_the_top(audio):
    out = audio.lines("setup", "set:call_step:7", "call:1:0", "idle", "control:hal_output:volume:1.3", "idle")
    i = out.index("audio-hal-bridge: call volume 7/7 (call start)")
    # Above the top step: back onto it, the step stays
    assert [line for line in out[i + 1:] if "bt state" not in line] == ["control hal_output channelVolumes=1.000,1.000"]


def test_call_volume_with_a_headset(audio):
    out = audio.lines("setup", "global:70:Metadata:metadata.name=default",
                      'meta:70:0:default.audio.sink:{"name":"bluez_output.AA.1"}', "volume-port",
                      "call:1:0", "idle", "call:0:0", "idle")
    assert "volume-port=none" in out
    # The headset sets its own volume: the HAL gets the step, no sink shows it
    assert not [line for line in out if line.startswith("control ")]
    assert "voice-volume=0.857" in out


def test_call_volume_sink_gone(audio):
    # The headphones were the keys' sink and went with the plug: nothing to show it on
    out = audio.lines("setup", "global:70:Metadata:metadata.name=default", "headphones:1:0", "phones-update",
                      'meta:70:0:default.audio.sink:{"name":"hal_headphones"}', "headphones:0:0",
                      "phones-update", "call:1:0", "idle", "call:0:0", "idle")
    assert not [line for line in out if line.startswith("control ")]
    assert "audio-hal-bridge: call volume 6/7 (call start)" in out


# ---- With role based volumes: the call channel's share of the output ----

def test_role_call_volume_follows_the_call_channel(audio):
    out = audio.lines("setup", "global:300:Node:node.name=input.loopback.sink.role.phone",
                      "role-props:300:Props:0.125:-", "idle",
                      "call:1:0", "idle",
                      "role-props:300:Props:1:0.343,0.343", "idle",
                      "call:0:0", "idle", "role-props:300:Props:1:1", "idle", "call-volume")
    assert "subscribe 300 Props" in out
    volumes = [line for line in out if line.startswith(("voice-volume", "audio-hal-bridge: call"))]
    # The output (the speaker at full volume) times the call channel: 0.5, then 0.7
    assert volumes == ["voice-volume=0.500",
                       "audio-hal-bridge: call volume 0.50 = output hal_output x call channel 0.50 (call start)",
                       "voice-volume=0.700",
                       "audio-hal-bridge: call volume 0.70 = output hal_output x call channel 0.70 (change)"]
    # No sink took the call volume: nothing to give back
    assert not [line for line in out if line.startswith("control ")]
    assert out[-1].startswith("call-volume step=6 on=0 port=none")


def test_role_call_channel_appears_during_a_call(audio):
    out = audio.lines("setup", "control:hal_output:channels:0.5,0.5", "idle", "call:1:0", "idle",
                      "global:300:Node:node.name=input.loopback.sink.role.phone",
                      "role-props:300:Props:0.125:-", "idle")
    i = out.index("role-phone-gain=0.125")
    # The keys' sink gets its media volume back, then the role volume takes over
    assert out[i + 1:] == ["control hal_output channelVolumes=0.500,0.500", "voice-volume=0.397",
                           "audio-hal-bridge: call volume 0.40 = output hal_output x call channel 0.50 (change)"]


def test_role_call_channel_goes_away(audio):
    out = audio.lines("setup", "global:300:Node:node.name=input.loopback.sink.role.phone",
                      "global:301:Node:node.name=input.loopback.sink.role.phone",
                      "role-props:300:Props:0.125:-", "remove:301", "call-volume", "remove:300", "call-volume",
                      "idle", "call:1:0", "idle")
    # Only the first is followed; it goes with WirePlumber: the call volume of its own again
    assert out.count("bind 300 Node") == 1 and "bind 301 Node" not in out
    assert "role=0.125" in out[out.index("proxy-destroy 300 Node") - 1]
    assert "role=-1.000" in out[out.index("proxy-destroy 300 Node") + 1]
    assert "audio-hal-bridge: call volume 6/7 (call start)" in out


@pytest.mark.parametrize("kind,args", [("Props", "-:-"), ("Other", "-:-"), ("int", "-:-"), ("null", "-:-")])
def test_role_props_without_a_volume(audio, kind, args):
    # Props without volumes, another param, no object, nothing: the gain stays unknown
    out = audio("setup", "global:300:Node:node.name=input.loopback.sink.role.phone", f"role-props:300:{kind}:{args}")
    assert "role-phone-gain=-1.000" in out


def test_role_props_volume_and_channels(audio):
    out = audio("setup", "global:300:Node:node.name=input.loopback.sink.role.phone",
                "role-props:300:Props:-:0.5,0.25", "role-props:300:Props:0.5:-", "role-props:300:Props:0.5:0.5")
    # The first channel stands for all
    assert [line for line in out.splitlines() if line.startswith("role-phone")] == [
        "role-phone-gain=0.500", "role-phone-gain=0.500", "role-phone-gain=0.250"]


def test_role_phone_without_proxy(audio):
    out = audio("setup", "fail:bind:300", "global:300:Node:node.name=input.loopback.sink.role.phone",
                "global:301:Node:node.name=input.loopback.sink.role.phone")
    # Not bound: the next one is tried
    assert "bind 300 Node" in out and "bind 301 Node" in out and "subscribe 301 Props" in out


# ---- The earpiece amplifier ----

@pytest.mark.parametrize("cmds,result,writes", [
    (["amp:0"], 0, ["amp=Off"]),
    (["amp:0", "amp:1"], 0, ["amp=Off", "amp=Music"]),
    (["alsa:Off,Music:0", "amp:0"], 0, []),                    # already as asked: not written
    (["alsa:-", "amp:0"], -2, []),                             # no such control
    (["alsa:Music,Receiver:0", "amp:0"], -2, []),              # no such item
    (["fail:alsa_item:0", "amp:0"], -2, []),                   # the item unreadable
    (["fail:alsa_open", "amp:0"], -1, []),
    (["fail:alsa_info", "amp:0"], -5, []),
    (["fail:alsa_read", "amp:0"], -5, []),
    (["fail:alsa_write", "amp:0"], -5, []),
])
def test_earpiece_amplifier(audio, cmds, result, writes):
    out = audio.lines(*cmds)
    assert [line for line in out if line.startswith("amp=")] == writes
    assert out[-1] == f"amp-set {cmds[-1][4:]}={result}"
    assert "alsa-open hw:0" in out


# ---- Timing helpers ----

@pytest.mark.parametrize("nsec,until", [(0, "+0s 50000000ns"), (949999999, "+0s 999999999ns"),
                                        (950000000, "+1s 0ns"), (999999999, "+1s 49999999ns")])
def test_deadline(audio, nsec, until):
    assert audio(f"deadline:{nsec}").strip() == f"deadline {until}"


@pytest.mark.parametrize("t,ms,result", [("5:0", 2000, "7.000000000"), ("5:999000000", 1, "6.000000000"),
                                         ("5:500000000", 1750, "7.250000000")])
def test_add_ms(audio, t, ms, result):
    assert audio(f"add-ms:{t}:{ms}").strip() == f"add-ms {result}"


# ---- The headset jack ----

def jack(audio, *cmds):
    return audio.lines("setup", "jack", "idle", *cmds)


def test_no_input_devices(audio):
    assert "jack none" in audio("setup", "jack")


def test_jack_found_among_input_devices(audio):
    d = audio.cwd / "input"
    d.mkdir()
    (d / "mouse0").write_text("")
    jack_device(audio.cwd, "mtk-kpd", 0)
    jack_device(audio.cwd, "Jack", 1)                          # too short to end in the name
    (d / "event2").symlink_to(d / "gone")                      # cannot be opened
    jack_device(audio.cwd, "mt6789-mt6359 Headset Jack", 3, states=[[SW_HEADPHONE_INSERT]])
    out = jack(audio)
    assert "jack found" in out
    assert f"hal connect={HEADPHONE}" in out


def test_jack_device_that_cannot_be_opened(audio):
    d = audio.cwd / "input"
    d.mkdir()
    (d / "event0").symlink_to(d / "gone")
    assert "jack none" in jack(audio)


def test_jack_without_a_name(audio):
    dev = jack_device(audio.cwd, "x", 0)
    (dev.parent / "event0.name").unlink()
    assert "jack none" in jack(audio)


def test_jack_headset_plugged_and_pulled(audio):
    jack_device(audio.cwd, "Headset Jack", 0, events=2,
                states=[[SW_HEADPHONE_INSERT, SW_MICROPHONE_INSERT], [SW_HEADPHONE_INSERT, SW_MICROPHONE_INSERT], []])
    out = jack(audio)
    hal = [line for line in out if line.startswith(("hal ", "out routing", "in routing"))]
    # Reported to the HAL as Android's audio policy does, before it is routed there; the
    # second report changed nothing: not told again
    assert hal == [f"hal connect={HEADSET}", f"hal connect={HEADSET_MIC}", f"out routing={HEADSET}",
                   f"in routing={HEADSET_MIC}", f"out routing={HEADSET}", f"in routing={HEADSET_MIC}",
                   f"hal disconnect={HEADSET}", f"hal disconnect={HEADSET_MIC}", f"out routing={SPEAKER}",
                   f"in routing={MIC}"]
    assert f"log wired device 0 -> {HEADSET:#x}" in out
    # Unplugged by the time the main loop looked: no headphones sink
    assert "stream-new hal_headphones" not in "\n".join(out)


@pytest.mark.parametrize("switch,device", [(SW_HEADPHONE_INSERT, HEADPHONE), (SW_LINEOUT_INSERT, HEADPHONE)])
def test_jack_headphones(audio, switch, device):
    jack_device(audio.cwd, "Headset Jack", 0, states=[[switch]])
    out = jack(audio, "call:0:0")
    assert f"hal connect={device}" in out and f"out routing={device}" in out and f"in routing={MIC}" in out
    assert not [line for line in out if line.startswith(f"hal connect={HEADSET_MIC}")]
    assert [line for line in out if line.startswith("stream-new hal_headphones")]


def test_wired_report_changes_device(audio):
    out = audio.lines("report-wired:8", "report-wired:8", "report-wired:4", "report-wired:0")
    assert [line for line in out if line.startswith("hal ")] == [
        "hal connect=8", "hal disconnect=8", "hal connect=4", "hal disconnect=4"]
