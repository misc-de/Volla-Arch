# audio-hal-bridge: the threads between the HAL and PipeWire. The playback thread (warm-up after
# standby, the mix of the sinks, the linger before standby, the echo reference, Bluetooth's
# volume, the earpiece amplifier in a speakerphone conversation), the capture thread (which
# microphone, the headset's source, the echo canceller, the graph's period) and the FM thread
# (opening the tuner's input, retrying, silence in real time, closing after a pause), and the
# graph cycles they run. The stand-in HAL's writes, reads and standby run scripted actions at
# their n-th call (at:<event>:<n>:<sync|async>:<command>), so each run takes the same course.

import re

import pytest

from dhw_audio_helpers import FM, MIC, audio, kv  # noqa: F401 (fixture)


def hal(out):
    """The output stream's side of a run, folded (xN)."""
    return [line for line in out.splitlines() if line.startswith(("out-", "out routing", "playback"))]


def playback(audio, *cmds):
    return audio("setup", "compact", *cmds, "run-playback")


def test_playback_warm_up_linger_standby(audio):
    out = playback(audio, "at:out-standby:1:async:on:hal_output", "at:out-write:9:sync:off:hal_output",
                   "at:out-standby:2:async:quit")
    # 16384 bytes per transfer: 85 ms; 500 ms of silence before the sound (the amplifiers start
    # late), then the sound, 3 s of silence after it, then standby
    assert hal(out) == ["out-standby", "out routing=2", "out-write silence x6", "out-write data 1000..1000 x3",
                        "out-write silence x35", "out-standby", "playback thread ended"]


def test_playback_runs_the_graph_per_transfer(audio):
    # The graph's quantum is a quarter of the transfer: four cycles per transfer
    out = audio("setup", "compact", "at:out-standby:1:async:on:hal_output", "at:out-write:8:sync:off:hal_output",
                "at:out-standby:2:async:quit", "run-playback", "port:hal_output")
    assert "port hal_output active=0 cycles=8 " in out


def test_playback_mixes_the_sinks(audio):
    out = playback(audio, "stream:hal_earpiece:value:20000", "stream:hal_output:value:20000",
                   "at:out-standby:1:async:on:hal_earpiece", "at:out-write:8:sync:on:hal_output",
                   "at:out-write:10:sync:off:hal_output", "at:out-write:10:sync:off:hal_earpiece",
                   "at:out-standby:2:async:quit")
    # The earpiece alone: routed there; the speaker joins: the speaker for both, no new warm-up,
    # the mix clipped
    assert hal(out) == ["out-standby", "out routing=1", "out-write silence x6", "out-write data 20000..20000 x2",
                        "out routing=2", "out-write data 32767..32767 x2", "out-write silence x35", "out-standby",
                        "playback thread ended"]


def test_playback_mix_clips_below(audio):
    # The speaker joins at a transfer of its own (sync), as in test_playback_mixes_the_sinks:
    # turning both on in the same standby wait left it to the thread timing whether the first
    # transfer already had both
    out = playback(audio, "stream:hal_earpiece:value:-20000", "stream:hal_output:value:-20000",
                   "at:out-standby:1:async:on:hal_earpiece", "at:out-write:8:sync:on:hal_output",
                   "at:out-write:10:sync:quit")
    assert hal(out)[:6] == ["out-standby", "out routing=1", "out-write silence x6",
                            "out-write data -20000..-20000 x2", "out routing=2",
                            "out-write data -32768..-32768 x2"]


def test_playback_sounds_in_a_row_skip_the_warm_up(audio):
    out = playback(audio, "at:out-standby:1:async:on:hal_output", "at:out-write:8:sync:off:hal_output",
                   "at:out-write:20:sync:on:hal_output", "at:out-write:22:sync:off:hal_output",
                   "at:out-standby:2:async:quit")
    # The second sound within the linger: at once, and what the first left queued is gone
    assert hal(out) == ["out-standby", "out routing=2", "out-write silence x6", "out-write data 1000..1000 x2",
                        "out-write silence x12", "out-write data 1000..1000 x2", "out-write silence x35",
                        "out-standby", "playback thread ended"]


def test_playback_on_bluetooth_undoes_the_volume(audio):
    # On SCO the headset sets the volume: the speaker's volume (0.5) applied by PipeWire is undone
    out = playback(audio, "control:hal_output:volume:0.5", "sco:1", "at:out-standby:1:async:on:hal_output",
                   "at:out-write:8:sync:quit")
    assert "out-write data 2000..2000 x2" in hal(out)
    out = playback(audio, "control:hal_output:volume:0.5", "at:out-standby:1:async:on:hal_output",
                   "at:out-write:8:sync:quit")
    assert "out-write data 1000..1000 x2" in hal(out)


def test_playback_echo_reference(audio):
    out = playback(audio, "control:hal_output:volume:0.5", "aec:1", "at:out-standby:1:async:on:hal_output",
                   "at:out-write:8:sync:ref", "at:out-write:8:sync:aec:0", "at:out-write:9:sync:ref",
                   "at:out-write:9:sync:quit")
    refs = [line for line in out.splitlines() if line.startswith("ref ")]
    # Two transfers of 4096 frames: 17 blocks of 10 ms, 32 frames waiting; the reference has the
    # volume undone (the canceller takes it at the level it plays); off: emptied
    assert refs == ["ref count=17 first=2000 fill=32", "ref count=0 first=0 fill=0"]
    # The sound itself keeps its volume
    assert set(line for line in hal(out) if "data" in line) == {"out-write data 1000..1000"}


def test_playback_echo_reference_clips(audio):
    out = playback(audio, "stream:hal_output:value:30000", "control:hal_output:volume:0.5", "aec:1",
                   "at:out-standby:1:async:on:hal_output", "at:out-write:7:sync:ref", "at:out-write:7:sync:quit")
    assert "ref count=8 first=32767 fill=256" in out


def test_playback_speakerphone_turns_the_earpiece_amplifier_off(audio):
    # A conversation over the speaker (routing sets the switch from the canceller's state)
    out = playback(audio, "aec:1", "at:out-standby:1:async:on:hal_output",
                   "at:out-write:36:sync:speakerphone:0", "at:out-write:37:sync:quit")
    lines = out.splitlines()
    # Off after the first transfer, checked again every 24 transfers (the HAL sets its path when
    # it routes); back on with the speaker path when the conversation ends
    assert [line for line in lines if line.startswith("amp=")] == ["amp=Off", "amp=Music"]
    assert lines.count("alsa-open hw:0") == 3
    assert lines.index("amp=Off") < lines.index("out-write data 1000..1000 x23")


def test_playback_speakerphone_amplifier_retried(audio):
    out = playback(audio, "aec:1", "fail:alsa_open*3", "at:out-standby:1:async:on:hal_output",
                   "at:out-write:11:sync:quit")
    lines = out.splitlines()
    # Not switched yet: tried after every transfer until it is
    assert [line for line in lines if line.startswith(("alsa-open", "amp="))] == ["alsa-open hw:0"] * 4 + ["amp=Off"]


def test_playback_speakerphone_ends_on_another_route(audio):
    out = playback(audio, "aec:1", "at:out-standby:1:async:on:hal_output",
                   "at:out-write:8:sync:speakerphone:0", "at:out-write:8:sync:speaker-routed:0",
                   "at:out-write:9:sync:quit")
    # On another route the HAL set the amplifier itself: left alone
    assert [line for line in out.splitlines() if line.startswith("amp=")] == ["amp=Off"]


def test_playback_underrun_stays_silence(audio):
    # The graph runs but never completes its cycle: what came is played, the rest is silence
    out = playback(audio, "stream:hal_output:nodone:1", "at:out-standby:1:async:on:hal_output",
                   "at:out-write:7:sync:quit")
    assert hal(out)[-2:] == ["out-write data 1000..0", "playback thread ended"]


def test_playback_graph_driven_elsewhere(audio):
    # Another driver runs the graph and it did not run in time: silence, never stale data
    out = playback(audio, "stream:hal_output:driving:0", "at:out-standby:1:async:on:hal_output",
                   "at:out-write:7:sync:quit")
    assert hal(out)[-2:] == ["out-write silence x7", "playback thread ended"]


def test_playback_stops_during_the_warm_up(audio):
    out = playback(audio, "at:out-standby:1:async:on:hal_output", "at:out-write:2:sync:quit")
    assert hal(out) == ["out-standby", "out routing=2", "out-write silence x2", "playback thread ended"]


@pytest.mark.parametrize("cmds,result", [
    ([], "cycle hal_output=1 cycles=1"),
    (["stream:hal_output:nodone:1"], "cycle hal_output=0 cycles=0"),     # the cycle did not complete
    (["stream:hal_output:driving:0"], "cycle hal_output=0 cycles=0"),    # another driver, no cycle
    # Another driver: its next cycle came
    (["stream:hal_output:driving:0", "async:delay:20:process:hal_output"], "cycle hal_output=1 cycles=0"),
    (["drop-stream:hal_output"], "cycle hal_output=0 cycles=0"),        # the node went away
    (["stream:hal_output:nodone:1", "quit"], "cycle hal_output=0 cycles=0"),  # stopping: no wait
])
def test_run_cycle(audio, cmds, result):
    out = audio("setup", "active:hal_output:1", *cmds, "cycle:hal_output")
    assert out.splitlines()[-1] == result


# ---- Capture ----

def delivered(out, node="hal_input"):
    return [line.split(" ", 2)[2] for line in out.splitlines() if line.startswith(f"deliver {node} ")]


def capture(audio, *cmds, pre=()):
    return audio(*pre, "setup", *cmds, "run-capture")


def test_capture_lower_microphone_upper_for_speakerphone(audio):
    out = capture(audio, "at:in-standby:1:async:on:hal_input", "at:in-read:2:sync:speakerphone:1",
                  "at:in-read:3:sync:speakerphone:0", f"at:in-read:3:sync:fail:read:{MIC:#x}*1",
                  "at:in-read:5:sync:off:hal_input", "at:in-standby:2:async:quit")
    # One HAL period (1024 stereo frames) per read; channel 0 is the lower microphone, channel 1
    # the upper one, for a conversation over the speaker; a failed read gives nothing
    assert delivered(out) == ["2048 offset=0 first=100 last=100 stride=2", "2048 offset=0 first=200 last=200 stride=2",
                              "2048 offset=0 first=100 last=100 stride=2"]
    lines = out.splitlines()
    assert lines.count("in-standby") == 2 and lines[-1] == "capture thread ended"
    assert lines.index("in-standby") < lines.index("deliver hal_input 2048 offset=0 first=100 last=100 stride=2")


def test_capture_mono_input(audio):
    out = capture(audio, "at:in-standby:1:async:on:hal_input", "at:in-read:1:sync:speakerphone:1",
                  "at:in-read:3:sync:off:hal_input", "at:in-standby:2:async:quit", pre=[f"fail:open_in:{MIC:#x}:0xc"])
    # One microphone only: no upper one to switch to
    assert delivered(out) == ["4096 offset=0 first=100 last=100 stride=2"] * 2


def test_capture_follows_the_graphs_period(audio):
    out = capture(audio, "stream:hal_input:requested:512", "at:in-standby:1:async:on:hal_input",
                  "at:in-read:2:sync:off:hal_input", "at:in-standby:2:async:quit")
    # The graph takes 512 frames per cycle: two cycles per HAL period (the second read came
    # after the end)
    assert delivered(out) == ["1024 offset=0 first=100 last=100 stride=2"] * 2


def test_capture_echo_cancelled(audio):
    out = capture(audio, "play:480:7", "play:480:8", "aec:1", "at:in-standby:1:async:on:hal_input",
                  "at:in-read:3:sync:off:hal_input", "at:in-standby:2:async:quit")
    # Whole 10 ms blocks only (1024 frames: 2 blocks, 64 frames wait), so the first read is less
    # than a period; each block with its reference block (the stand-in canceller marks the
    # block's first frame with it)
    assert delivered(out) == ["3840 offset=0 first=7 last=100 stride=2"]


def test_capture_headset_source(audio):
    out = capture(audio, "connect-port:hal_headset_input", "at:in-standby:1:async:on:hal_headset_input",
                  "at:in-read:1:sync:on:hal_input", "at:in-read:2:sync:sco:1", "at:in-read:3:sync:off:hal_input",
                  "at:in-read:3:sync:off:hal_headset_input", "at:in-standby:2:async:quit")
    # Silence until the HAL is on SCO, never the phone's microphone; then what the HAL reads
    assert delivered(out, "hal_headset_input") == ["2048 offset=0 first=0 last=0 stride=2",
                                                   "2048 offset=0 first=100 last=100 stride=2"]
    # The microphone's own source from the read during which it came
    assert delivered(out) == ["2048 offset=0 first=100 last=100 stride=2"] * 2


def test_capture_graph_that_does_not_run(audio):
    out = capture(audio, "stream:hal_input:driving:0", "at:in-standby:1:async:on:hal_input",
                  "at:in-read:2:sync:off:hal_input", "at:in-standby:2:async:quit")
    # Nobody took the data: nothing delivered, the thread goes on with the next read
    assert delivered(out) == [] and out.splitlines()[-1] == "capture thread ended"


# ---- FM ----

def fm(audio, *cmds, timeout=20):
    return audio("setup", "connect-port:hal_fm_input", "compact", *cmds, "run-fm", timeout=timeout)


def test_fm_retries_and_plays_silence_meanwhile(audio):
    out = fm(audio, f"fail:open_in:{FM:#x}:0xc*1", "on:hal_fm_input", "at:fm-read:3:sync:off:hal_fm_input",
             "at:fm-close:1:async:quit")
    lines = out.splitlines()
    # Refused: silence in real time (1024 frames every 21 ms) for a second, then the tuner's sound
    assert f"open-in {FM:#x} handle=3 mask=0xc rate=48000 source=1998" in lines
    assert "audio-hal-bridge: no FM input (-22), the HAL suggests 48000 Hz, mask 0xc, format 0x1" in lines
    i = lines.index("audio-hal-bridge: no FM input (-22), the HAL suggests 48000 Hz, mask 0xc, format 0x1")
    silent = re.fullmatch(r"deliver hal_fm_input 4096 offset=0 first=0 last=0 stride=4 x(\d+)", lines[i + 1])
    assert silent and 20 <= int(silent.group(1)) <= 60
    assert lines[i + 2:i + 5] == [f"open-in {FM:#x} handle=3 mask=0xc rate=48000 source=1998",
                                  "log FM input open, HAL buffer 4096 bytes",
                                  "deliver hal_fm_input 4096 offset=0 first=300 last=300 stride=4 x2"]
    # Stopped: the input closes after the pause, then the thread waits
    assert lines[-3:] == [f"close-in {FM:#x}", "log FM input closed", "fm thread ended"]


def test_fm_back_within_the_pause_keeps_the_input(audio):
    out = fm(audio, "on:hal_fm_input", "at:fm-read:2:sync:off:hal_fm_input",
             "at:fm-read:2:async:delay:300:on:hal_fm_input", "at:fm-read:4:sync:quit")
    lines = out.splitlines()
    assert lines.count(f"open-in {FM:#x} handle=3 mask=0xc rate=48000 source=1998") == 1
    # Stopping with the input open closes it
    assert lines[-2:] == [f"close-in {FM:#x}", "fm thread ended"]
    # The reads while it streams (not the one after which it stopped, nor the last)
    assert [line for line in lines if line.startswith("deliver")] == [
        "deliver hal_fm_input 4096 offset=0 first=300 last=300 stride=4"] * 2


def test_fm_input_at_another_rate_not_taken(audio):
    out = fm(audio, f"fail:in_mismatch:{FM:#x}:0xc", "on:hal_fm_input", "at:fm-open:1:async:delay:100:quit")
    lines = out.splitlines()
    assert "audio-hal-bridge: FM input at 44100 Hz, mask 0xc, format 0x1: not taken" in lines
    assert lines[lines.index("audio-hal-bridge: FM input at 44100 Hz, mask 0xc, format 0x1: not taken") + 1] == \
        f"close-in {FM:#x}"
    assert lines[-1] == "fm thread ended" and "log FM input open" not in out


def test_fm_failed_reads_give_silence(audio):
    out = fm(audio, f"fail:read:{FM:#x}*2", "on:hal_fm_input", "at:fm-read:4:sync:quit")
    assert [line for line in out.splitlines() if line.startswith("deliver")] == [
        "deliver hal_fm_input 4096 offset=0 first=0 last=0 stride=4 x2",
        "deliver hal_fm_input 4096 offset=0 first=300 last=300 stride=4"]


def test_fm_graph_that_does_not_run(audio):
    out = fm(audio, "stream:hal_fm_input:driving:0", "on:hal_fm_input", "at:fm-read:2:sync:quit")
    # Nobody took the sound: nothing delivered, the thread goes on with the next read
    assert "deliver" not in out and out.splitlines()[-2:] == [f"close-in {FM:#x}", "fm thread ended"]
