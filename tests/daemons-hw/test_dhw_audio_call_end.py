# audio-hal-bridge: the jingle at the end of a call. Loading the sound (16 bit stereo PCM WAV at
# 48 kHz, at most 3 s), feedbackd's "silent" profile, the start with the call's volume (or the
# headset's own over SCO), the mix on top of the sinks in the playback thread, normal mode after
# the jingle has sounded (or after the safety timeout), a call that starts meanwhile, and the same
# through callaudiod's SelectMode on the test's own D-Bus session bus.

import shutil
import subprocess
import wave

import pytest

from conftest import PKG
from dhw_audio_helpers import Bus, audio  # noqa: F401 (fixture)

RATE = 48000
CHUNK_FRAMES = 4096  # the stand-in HAL's transfer: 16384 bytes, stereo, 16 bit


@pytest.fixture(scope="module")
def bus(tmp_path_factory):
    b = Bus(tmp_path_factory.mktemp("bus"))
    yield b.address
    b.stop()


def write_wav(path, frames, value=1000, channels=2, rate=RATE, width=2, last=None):
    samples = [value] * (frames * channels)
    if last is not None and samples:
        samples[-1] = last
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"".join(s.to_bytes(width, "little", signed=True) for s in samples))


def no_schemas(cwd):
    """Settings schemas only from the test directory (none unless a test compiles one)."""
    d = cwd / "data"
    (d / "glib-2.0" / "schemas").mkdir(parents=True, exist_ok=True)
    return {"XDG_DATA_DIRS": d}


def feedbackd_schema(cwd, profile, key="profile"):
    """feedbackd's schema as on the phone (type s with choices), its default the profile given."""
    if shutil.which("glib-compile-schemas") is None:
        pytest.skip("no glib-compile-schemas")
    env = no_schemas(cwd)
    d = env["XDG_DATA_DIRS"] / "glib-2.0" / "schemas"
    (d / "org.sigxcpu.feedbackd.gschema.xml").write_text(f"""<schemalist>
  <schema id="org.sigxcpu.feedbackd" path="/org/sigxcpu/feedbackd/">
    <key name="{key}" type="s">
      <choices><choice value='full'/><choice value='quiet'/><choice value='silent'/></choices>
      <default>'{profile}'</default>
    </key>
  </schema>
</schemalist>
""")
    subprocess.run(["glib-compile-schemas", str(d)], check=True)
    return env


def hal(out):
    return [line for line in out.splitlines() if line.startswith(("out-", "out routing", "playback"))]


def lines_of(out, *prefixes):
    return [line for line in out.splitlines() if line.startswith(prefixes)]


# ---- the sound ----

def test_jingle_loaded(audio):
    write_wav(audio.cwd / "j.wav", 4800, value=1200, last=-7)
    assert "jingle-load frames=4800 first=1200 last=-7" in audio("jingle-load:j.wav")


def test_the_packaged_jingle_loads(audio):
    # The file the package installs: one second at 48 kHz, starting and ending in silence
    shutil.copy(PKG / "audio-hal-bridge" / "call-end.wav", audio.cwd / "packaged.wav")
    out = audio("jingle-load:packaged.wav")
    assert "jingle-load frames=48000 first=0 last=0" in out


def test_jingle_after_other_chunks(audio):
    # A LIST chunk of odd length before fmt: chunks are padded to even sizes
    write_wav(audio.cwd / "plain.wav", 10)
    data = (audio.cwd / "plain.wav").read_bytes()
    body = b"LIST" + (3).to_bytes(4, "little") + b"abc\0" + data[12:]
    (audio.cwd / "j.wav").write_bytes(b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WAVE" + body)
    assert "jingle-load frames=10 first=1000 last=1000" in audio("jingle-load:j.wav")


@pytest.mark.parametrize("kind", ["mono", "rate", "width", "long", "missing", "garbage", "truncated", "no-fmt"])
def test_jingle_refused(audio, kind):
    path = audio.cwd / "j.wav"
    if kind == "mono":
        write_wav(path, 100, channels=1)
    elif kind == "rate":
        write_wav(path, 100, rate=44100)
    elif kind == "width":
        write_wav(path, 100, value=10, width=1)
    elif kind == "long":
        write_wav(path, 3 * RATE + 1)
    elif kind == "garbage":
        path.write_bytes(b"OggS" + bytes(60))
    elif kind == "truncated":
        write_wav(path, 100)
        path.write_bytes(path.read_bytes()[:-8])  # the data chunk says more than there is
    elif kind == "no-fmt":
        path.write_bytes(b"RIFF" + (16).to_bytes(4, "little") + b"WAVE" + b"data" + (4).to_bytes(4, "little") +
                         bytes(4))
    out = audio("jingle-load:j.wav")
    assert "jingle-load frames=0 first=0 last=0" in out
    if kind == "missing":
        assert "audio-hal-bridge: no call end sound j.wav" in out
    else:
        assert "audio-hal-bridge: call end sound j.wav is not 16 bit stereo PCM at 48000 Hz up to 3 s" in out


# ---- feedbackd's profile ----

def test_feedback_silent_without_feedbackd(audio):
    assert "feedback-silent=0" in audio("feedback-silent", **no_schemas(audio.cwd))


@pytest.mark.parametrize("profile,key,silent", [("silent", "profile", 1), ("quiet", "profile", 0),
                                                ("full", "profile", 0), ("silent", "other", 0)])
def test_feedback_silent(audio, profile, key, silent):
    assert f"feedback-silent={silent}" in audio("feedback-silent", **feedbackd_schema(audio.cwd, profile, key))


# ---- start, cancel, the volume ----

def start(audio, *cmds, **env):
    return audio("compact", "route:0:0:0:1:0:0:0", *cmds, "call-end:start", **(env or no_schemas(audio.cwd)))


def test_no_jingle_no_wait(audio):
    out = start(audio)
    assert "call-end-start=0" in out
    assert "call-end ending=0 gen=0 timeout=0 call=1 playing=0 pos=0 gain=0.000 jingle-gen=0" in out


def test_silent_profile_no_wait(audio):
    out = start(audio, "jingle:100:1000", **feedbackd_schema(audio.cwd, "silent"))
    assert "call-end-start=0" in out and "timeout-add" not in out


@pytest.mark.parametrize("cmds,gain", [
    (["set:call_step:7"], "1.000"),
    (["set:call_step:3"], "0.079"),                                         # (3/7)^3
    (["set:role_phone_gain:0.5", "set:voice_applied:0.6"], "0.216"),        # role based: what the HAL got
    (["set:role_phone_gain:0.5", "set:voice_applied:0"], "0.003"),          # never below the first step
])
def test_start_with_the_call_volume(audio, cmds, gain):
    out = start(audio, "jingle:4800:1000", *cmds)
    assert "call-end-start=1" in out
    # Call mode stays; the safety timeout: the jingle's 100 ms, the warm-up, the slack
    assert "timeout-add 2600" in out and "set-mode=0" not in out
    assert f"call-end ending=1 gen=1 timeout=1 call=1 playing=1 pos=0 gain={gain} jingle-gen=1" in out
    assert f"audio-hal-bridge: call end sound, gain {float(gain):.2f}" in out


def test_start_on_bluetooth_leaves_the_volume_to_the_headset(audio):
    out = audio("compact", "jingle:4800:1000", "set:call_step:2", "route:1:0:0:1:0:0:0", "call-end:start",
                **no_schemas(audio.cwd))
    assert "call-end ending=1 gen=1 timeout=1 call=1 playing=1 pos=0 gain=1.000 jingle-gen=1" in out


def test_cancel(audio):
    out = audio("compact", "route:0:0:0:1:0:0:0", "jingle:4800:1000", "set:call_step:6", "call-end:start",
                "call-end:cancel", **no_schemas(audio.cwd))
    assert "timeout-remove 2600" in out
    assert "audio-hal-bridge: call end sound cut short: a call again" in out
    assert out.splitlines()[-1] == "call-end ending=0 gen=2 timeout=0 call=1 playing=0 pos=0 gain=0.630 jingle-gen=1"


def test_safety_timeout(audio):
    out = audio("compact", "route:0:0:0:1:0:0:0", "jingle:4800:1000", "call-end:start", "fire:2600", "call-end:state",
                **no_schemas(audio.cwd))
    lines = out.splitlines()
    i = lines.index("audio-hal-bridge: call end sound not through in time")
    assert "set-mode=0" in lines[i:] and "audio-hal-bridge: normal mode" in lines[i:]
    # The input is opened anew after the call, as without the jingle
    assert "close-in 0x80000004" in lines[i:]
    assert lines[-1].startswith("call-end ending=0 gen=1 timeout=0 call=0 playing=0")


# ---- the playback thread ----

def test_jingle_played_then_normal_mode(audio):
    out = audio("setup", "compact", "route:0:0:0:1:0:0:0", "set:call_step:7", "jingle:5000:1000", "call-end:start",
                "at:out-standby:1:async:quit", "run-playback", "idle", "fire:200", "call-end:state",
                **no_schemas(audio.cwd))
    # Out of standby: the warm-up, the jingle (one whole transfer, the rest of it with silence
    # after it), the linger, standby; nothing streams in any sink
    assert hal(out)[:5] == ["out routing=1", "out-write silence x6", "out-write data 1000..1000", "out-write data 1000..0",
                            "out-write silence x35"]
    lines = out.splitlines()
    i = lines.index("timeout-add 200")
    j = lines.index("audio-hal-bridge: call end sound played")
    assert i < j and "timeout-remove 2604" in lines[i:j + 1]
    assert "set-mode=0" in lines[j:] and "audio-hal-bridge: normal mode" in lines[j:]
    assert lines[-1].startswith("call-end ending=0 gen=1 timeout=0 call=0 playing=0 pos=5000")


def test_jingle_on_top_of_a_sink(audio):
    out = audio("setup", "compact", "route:0:0:0:1:0:0:0", "set:call_step:7", "jingle:4096:30000",
                "stream:hal_earpiece:value:20000", "on:hal_earpiece", "call-end:start",
                "at:out-write:7:sync:off:hal_earpiece", "at:out-standby:1:async:quit", "run-playback",
                **no_schemas(audio.cwd))
    assert "out-write data 32767..32767" in hal(out)


def test_jingle_of_an_ended_call_does_not_end_the_next(audio):
    # The jingle of the first end sounded, then a call came and ended again: the first one's
    # tail must not end the second one's call mode early
    out = audio("setup", "compact", "route:0:0:0:1:0:0:0", "jingle:100:1000", "call-end:start",
                "at:out-standby:1:async:quit", "run-playback", "idle", "call-end:cancel", "call-end:start",
                "fire:200", "call-end:state", **no_schemas(audio.cwd))
    assert "audio-hal-bridge: call end sound played" not in out
    assert out.splitlines()[-1].startswith("call-end ending=1 gen=3 timeout=1 call=1 playing=1")


# ---- through callaudiod's API ----

def test_select_mode_plays_the_jingle_first(audio, bus):
    write_wav(audio.cwd / "call-end.wav", 4800)
    r = audio.main(["dbus-wait", "dbus:SelectMode:(uint32 1,)", "barrier",
                    "dbus:SelectMode:(uint32 0,)", "dbus:SelectMode:(uint32 0,)", "barrier",
                    "delay:500:barrier", "fire:200", "barrier", "dbus-get:AudioMode", "sigterm"],
                   bus=bus, **no_schemas(audio.cwd))
    out = r.stdout
    lines = out.splitlines()
    # Both replies at once; one jingle; normal mode only after it sounded
    assert lines_of(out, "reply")[:3] == ["reply SelectMode (true,)"] * 3
    assert out.count("audio-hal-bridge: call end sound, gain 0.63") == 1
    j = lines.index("audio-hal-bridge: call end sound played")
    assert lines.index("audio-hal-bridge: call mode") < j
    assert lines.index("audio-hal-bridge: normal mode") > j
    assert lines_of(out, "reply AudioMode") == ["reply AudioMode (<uint32 0>,)"]


def test_select_mode_call_again_cuts_it_short(audio, bus):
    write_wav(audio.cwd / "call-end.wav", 4800)
    r = audio.main(["dbus-wait", "dbus:SelectMode:(uint32 1,)", "barrier", "dbus:SelectMode:(uint32 0,)",
                    "dbus:SelectMode:(uint32 1,)", "barrier", "dbus-get:AudioMode", "sigterm"],
                   bus=bus, **no_schemas(audio.cwd))
    out = r.stdout
    assert "audio-hal-bridge: call end sound cut short: a call again" in out
    assert "audio-hal-bridge: normal mode" not in out
    assert lines_of(out, "reply AudioMode") == ["reply AudioMode (<uint32 1>,)"]
