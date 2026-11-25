# plinius-ambient, the client (plinius-ambient.c) with its main, against stand-ins for the
# compositor and EGL (ambient/fakewl.c), GL (ambient/fakegl.c) and the system bus (in
# ambient/client_harness.c), the settings in GSettings' memory backend. Covered: the surface
# phoc-plinius draws under everything (bottom layer, no input, the whole screen), drawn at the
# panel's own pixels or half of them, the frame pacing by frame callbacks, the stop when they
# stay away and the one frame a second at rest, the sensors claimed only while an effect that
# follows them is on screen (gravity, motion, the game rotation vector with the fallback to the
# rotation vector), their values turned into the screen's axes, the display state from MCE, the
# settings (the old "panorama" effect, the video and the 360° photo excluding each other, a new
# background picture making them step aside), and leaving the compositor cleanly.

import math
import shutil
import subprocess

import pytest

from ambient_helpers import AMBIENT_FAKES, build, draws, gl
from dhw_harness import kv, run

BACKGROUND_SCHEMA = """<schemalist>
  <schema id="org.gnome.desktop.background" path="/org/gnome/desktop/background/">
    <key name="picture-uri" type="s"><default>''</default></key>
    <key name="picture-uri-dark" type="s"><default>''</default></key>
    <key name="picture-options" type="s"><default>'zoom'</default></key>
    <key name="primary-color" type="s"><default>'#000000'</default></key>
  </schema>
</schemalist>
"""


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    d = tmp_path_factory.mktemp("ambient-client")
    return build(d, "client", (AMBIENT_FAKES / "client_harness.c").read_text(), [],
                 included=["plinius-ambient.c"],
                 uncounted=["effects.c", "render.c", "panorama.c", "layered.c", "video.c"],
                 extra=[AMBIENT_FAKES / "fakewl.c"],
                 pkgs=("gio-unix-2.0", "gdk-pixbuf-2.0", "gstreamer-1.0", "gstreamer-app-1.0",
                       "gstreamer-video-1.0"),
                 flags=[f"-I{AMBIENT_FAKES / 'wl'}", "-DG_DISABLE_CAST_CHECKS", "-Wno-deprecated-declarations"])


@pytest.fixture(scope="module")
def schemas(tmp_path_factory):
    if not shutil.which("glib-compile-schemas"):
        pytest.skip("no glib-compile-schemas")
    with_bg = tmp_path_factory.mktemp("schemas")
    from ambient_helpers import DIR
    shutil.copy(DIR / "org.plinius.ambient.gschema.xml", with_bg)
    (with_bg / "org.gnome.desktop.background.gschema.xml").write_text(BACKGROUND_SCHEMA)
    subprocess.run(["glib-compile-schemas", str(with_bg)], check=True)
    without_bg = tmp_path_factory.mktemp("schemas-plain")
    shutil.copy(DIR / "org.plinius.ambient.gschema.xml", without_bg)
    subprocess.run(["glib-compile-schemas", str(without_bg)], check=True)
    return {"bg": with_bg, "plain": without_bg}


@pytest.fixture
def client(exe, schemas, tmp_path):
    def call(*args, env=None, check=True, background=True):
        e = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "XDG_DATA_DIRS": str(tmp_path / "none"),
             "XDG_CACHE_HOME": str(tmp_path / "cache"),
             "GSETTINGS_SCHEMA_DIR": str(schemas["bg" if background else "plain"]),
             "GSETTINGS_BACKEND": "memory", "G_MESSAGES_DEBUG": "plinius-ambient",
             "GST_REGISTRY": str(tmp_path / "registry.bin")}
        e.update(env or {})
        return run(exe, *args, env=e, check=check, timeout=30)
    return call


def lines(out, prefix):
    return [line for line in out.splitlines() if line.startswith(prefix)]


def calls(out):
    """The sensor and MCE methods called, in order"""
    return [line.split()[-1] for line in lines(out, "dbus call ")]


def states(out, key):
    return [line.split("=", 1)[1] for line in out.splitlines() if line.startswith(key + "=")]


# ---- Starting ----

def test_without_compositor(client):
    r = client(env={"FAKEWL_NO_DISPLAY": "1"}, check=False)
    assert r.returncode == 1 and "No Wayland compositor" in r.stderr


def test_compositor_without_layer_shell(client):
    r = client(env={"FAKEWL_GLOBALS": "wl_compositor,wl_output"}, check=False)
    assert r.returncode == 1 and "The compositor has no wlr-layer-shell" in r.stderr


def test_no_effect_no_surface(client):
    out = client("run", "wait:50", "state", "quit").stdout
    s = kv(out)
    assert s["effect"] == "none" and s["surface"] == "0" and s["exit"] == "0"
    # The globals bound, one output only; the display state asked from MCE without starting it
    assert lines(out, "wl bind") == ["wl bind wl_compositor 4", "wl bind wl_output 2",
                                     "wl bind zwlr_layer_shell_v1 2", "wl bind wp_viewporter 1"]
    assert "dbus call com.nokia.mce /com/nokia/mce/request get_display_status" in out
    assert lines(out, "wl create surface") == [] and lines(out, "egl ") == []


def test_a_second_output_is_ignored(client):
    out = client("run", "quit", env={"FAKEWL_GLOBALS": "wl_compositor,wl_output,wl_output,zwlr_layer_shell_v1"}).stdout
    assert len(lines(out, "wl bind wl_output")) == 1


def test_surface_for_an_effect(client):
    out = client("set:effect:snow", "run", "configure:360x800", "state", "quit").stdout
    # Bottom layer, phoc-plinius's namespace, no input, anchored all round over the panels
    assert lines(out, "wl layer surface")[0].endswith("layer 1 namespace plinius-ambient output any")
    assert "wl input region empty" in out and "wl anchor 15" in out
    assert "wl exclusive zone -1" in out and "wl size 0x0" in out
    assert lines(out, "egl choose config") == ["egl choose config alpha 8"]
    assert lines(out, "egl create context") == ["egl create context version 2"]
    s = kv(out)
    # Snow is soft: half the panel's 1080 pixels across 360 logical ones
    assert s["configured"] == "1" and s["buffer"] == "540x1200" and s["scale"] == "1.5000"
    assert "wl viewport destination 360x800" in out and "wl buffer scale 1" in out
    assert "wl egl window 540x1200" in out and "egl swap interval 0" in out
    # A frame at once, in the buffer's size, paced by a frame callback
    assert lines(out, "egl swap") == ["egl swap interval 0", "egl swap 1"]
    assert [d["viewport"] for d in draws(out)] == ["540x1200"]
    assert lines(out, "wl ack configure") == ["wl ack configure 101"]
    assert s["frame_callback"] == "1"
    # Quit: the surface and EGL go
    assert s["exit"] == "0" and "egl terminate" in out
    assert lines(out, "wl live")[-1] == ("wl live surface 0 region 0 callback 0 viewport 0 layer 0 egl_window 0")


def test_full_resolution_effect_draws_the_panel_exactly(client):
    # Balls have sharp edges: exactly the panel's pixels, even at a fractional scale
    out = client("set:effect:balls", "run", "configure:476x1057", "state", "quit",
                 env={"FAKEWL_OUTPUT": "0,1080,2400,3"}).stdout
    s = kv(out)
    assert s["buffer"] == "1080x2400"
    assert float(s["scale"]) == pytest.approx(1080 / 476, rel=1e-4)


def test_without_viewporter_the_integer_scale(client):
    out = client("set:effect:snow", "run", "configure:360x800", "state", "quit",
                 env={"FAKEWL_GLOBALS": "wl_compositor,wl_output,zwlr_layer_shell_v1"}).stdout
    s = kv(out)
    assert s["buffer"] == "1080x2400" and s["scale"] == "3.0000"
    assert "wl buffer scale 3" in out and "wl viewport" not in out


def test_rotated_output(client):
    # Turned by 90°: the panel's width is its height now; the sensor's axes turn as well
    out = client("set:effect:balls", "run", "output:1,1080,2400,3", "configure:800x360", "state", "quit").stdout
    s = kv(out)
    assert s["buffer"] == "2400x1080" and s["screen_angle"] == "90"


def test_zero_configure_is_only_acked(client):
    out = client("set:effect:snow", "run", "configure:0x0", "state", "quit").stdout
    # Sent (1), acked, but nothing is configured yet (0)
    assert states(out, "configured") == ["1", "0"] and "wl ack configure 101" in out
    assert lines(out, "egl create surface") == []


# ---- EGL failures ----

@pytest.mark.parametrize("what,message", [
    ("display", "Cannot initialize EGL (0x3008)"),
    ("init", "Cannot initialize EGL (0x3001)"),
    ("config", "No EGL config with alpha"),
    ("context", "Cannot create a GLES 2 context (0x3005)"),
])
def test_egl_does_not_start(client, what, message):
    r = client("set:effect:snow", "run", "state", "quit", env={"FAKEEGL_FAIL": what})
    assert message in r.stderr
    assert kv(r.stdout)["surface"] == "0"


def test_egl_surface_cannot_be_made(client):
    r = client("set:effect:snow", "run", "configure:360x800", "state", "quit", env={"FAKEEGL_FAIL": "surface"})
    assert "Cannot create the EGL surface (0x300b)" in r.stderr
    s = kv(r.stdout)
    assert s["state"] == "0" and states(r.stdout, "configured")[-1] == "0"


def test_make_current_and_swap_failing(client):
    r = client("set:effect:snow", "run", "configure:360x800", "quit", env={"FAKEEGL_FAIL": "current"})
    assert "eglMakeCurrent failed (0x300e)" in r.stderr
    assert draws(r.stdout) == []
    r = client("set:effect:snow", "run", "configure:360x800", "quit", env={"FAKEEGL_FAIL": "swap"})
    assert "eglSwapBuffers failed (0x300d)" in r.stderr


# ---- Pacing ----

def test_frames_follow_the_callbacks(client):
    out = client("set:effect:snow", "run", "configure:360x800", "wait:100", "pending", "frame", "wait:100",
                 "frame", "wait:100", "state", "quit").stdout
    swaps = lines(out, "egl swap ")
    # No frame without a callback; one after each (snow: 30 per second)
    assert states(out, "pending") == ["1"]
    assert swaps[1:] == ["egl swap 1", "egl swap 2", "egl swap 3"]
    assert kv(out)["stalled"] == "0"


def test_stalled_without_callbacks(client):
    out = client("set:effect:balls", "run", "configure:360x800", "reply:ClaimGravity:ok", "wait:1150", "state",
                 "frame", "wait:30", "state", "quit").stdout
    stalled = states(out, "stalled")
    # A second without a callback: not on screen, the tilt sensor released; back with the next one
    assert stalled == ["1", "0"]
    assert calls(out).count("ClaimGravity") == 2 and calls(out).count("ReleaseGravity") == 1


def test_resting_effect_drawn_once_a_second(client, photo):
    # The 360° photo held still comes to rest: drawn about once a second only (the callbacks
    # still tell whether it is on screen)
    args = [f"set:panorama:{photo}", "setb:panorama-enabled:1", "run", "configure:360x800",
            "signal:GameOrientation:(0.0, 0.0, 0.0, 1.0)"]
    for _ in range(30):
        args += ["frame", "wait:50"]
    args += ["state", "quit"]
    out = client(*args).stdout
    assert states(out, "resting")[-1] == "1"
    # 1.5 s: a few frames until the photo is shown, then one a second
    assert len(lines(out, "egl swap ")) - 1 < 12


def test_wake_from_rest(client, photo):
    args = [f"set:panorama:{photo}", "setb:panorama-enabled:1", "run", "configure:360x800",
            "signal:GameOrientation:(0.0, 0.0, 0.0, 1.0)"]
    for _ in range(30):
        args += ["frame", "wait:50"]
    args += ["state", "frame", "signal:GameOrientation:(0.0, 0.0, 0.258819, 0.9659258)", "state", "quit"]
    out = client(*args).stdout
    # Turned by 30°: the view follows again, the next frame at the full rate
    assert states(out, "resting")[-2:] == ["1", "0"]
    assert states(out, "draw_timer")[-1] == "1"


def test_request_frame_wakes(client, photo):
    args = [f"set:panorama:{photo}", "setb:panorama-enabled:1", "run", "wake", "configure:360x800",
            "signal:GameOrientation:(0.0, 0.0, 0.0, 1.0)"]
    for _ in range(30):
        args += ["frame", "wait:50"]
    args += ["frame", "wake", "state", "quit"]
    out = client(*args).stdout
    # scene.wake (the video's new frame): moving again, a frame soon
    assert states(out, "resting")[-1] == "0" and states(out, "draw_timer")[-1] == "1"


def test_frame_costs_logged(client):
    args = ["set:effect:snow", "run", "configure:360x800"]
    for _ in range(170):
        args += ["frame", "wait:33"]
    args += ["quit"]
    r = client(*args)
    assert "frames in" in r.stdout + r.stderr


# ---- Sensors ----

def test_gravity_into_the_screen_axes(client):
    out = client("set:effect:balls", "run", "configure:360x800", "reply:ClaimGravity:ok",
                 "signal:Gravity:(0.0, 9.80665, 0.0)", "state",
                 "signal:Gravity:(9.80665, 0.0, 0.0)", "state", "quit").stdout
    g = [tuple(map(float, v.split(","))) for v in states(out, "gravity")]
    # Held upright: down the screen; the second sample smoothed by 0.6 towards lying on the side
    assert g[0] == pytest.approx((0, 9.80665), abs=1e-3)
    assert g[1] == pytest.approx((-9.80665 * 0.6, 9.80665 * 0.4), abs=1e-3)


@pytest.mark.parametrize("transform,expected", [(0, (-1, 0)), (1, (0, -1)), (2, (1, 0)), (3, (0, 1))])
def test_gravity_turns_with_the_output(client, transform, expected):
    # The phone on its right edge (the sensor's x pointing up): gravity to the device's right
    out = client("set:effect:balls", "run", f"output:{transform},1080,2400,3", "configure:360x800",
                 "signal:Gravity:(1.0, 0.0, 0.0)", "state", "quit").stdout
    g = tuple(map(float, kv(out)["gravity"].split(",")))
    assert g == pytest.approx(expected, abs=1e-4)


def test_motion_shakes_and_pushes(client):
    out = client("set:effect:water", "run", "configure:360x800", "signal:Motion:(30.0, 0.0, 0.0)", "state",
                 "quit").stdout
    s = kv(out)
    # Clipped to 12 m/s², squared and smoothed over 0.8 s; the change of speed in 20 ms
    assert float(s["shake"]) == pytest.approx(144 * (1 - math.exp(-0.02 / 0.8)), rel=1e-3)
    assert tuple(map(float, s["impulse"].split(","))) == pytest.approx((12 * 0.02, 0), abs=1e-4)


def test_motion_ignored_unless_claimed(client):
    out = client("set:effect:balls", "run", "configure:360x800", "signal:Motion:(30.0, 0.0, 0.0)", "state",
                 "quit").stdout
    assert kv(out)["shake"] == "0.0000"


def test_claims_follow_the_effect(client):
    out = client("set:effect:water", "run", "configure:360x800", "set:effect:snow", "set:effect:none", "quit").stdout
    # Water follows tilt and movement; snow neither; none: the surface goes
    assert calls(out) == ["get_display_status", "ClaimGravity", "ClaimMotion", "ReleaseGravity", "ReleaseMotion"]
    assert "wl destroy layer surface" in out
    assert lines(out, "dbus sync") == []


def test_released_when_quitting(client):
    out = client("set:effect:water", "run", "configure:360x800", "quit").stdout
    assert lines(out, "dbus sync") == ["dbus sync net.hadess.SensorProxy ReleaseGravity",
                                       "dbus sync net.hadess.SensorProxy ReleaseMotion"]


def test_claims_renewed_when_the_bridge_restarts(client):
    out = client("set:effect:water", "run", "configure:360x800", "appeared", "quit").stdout
    assert calls(out)[-2:] == ["ClaimGravity", "ClaimMotion"]


def test_sensor_not_available(client):
    r = client("set:effect:balls", "run", "configure:360x800", "reply:ClaimGravity:error", "state", "quit")
    assert "Sensor not available (ClaimGravity): No such method ClaimGravity" in r.stderr


def test_without_a_system_bus(client):
    out = client("set:effect:water", "run", "configure:360x800", "state", "quit",
                 env={"FAKE_NO_BUS": "1"}).stdout
    s = kv(out)
    assert calls(out) == [] and s["gravity_claimed"] == "0" and s["display_on"] == "1"
    assert len(lines(out, "egl swap ")) >= 2


# ---- The 360° photo and its orientation ----

@pytest.fixture
def photo(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    p = tmp_path / "pano.png"
    PIL.new("RGB", (64, 32), (1, 2, 3)).save(p)
    return p


def test_orientation_with_fallback(client, photo):
    r = client(f"set:panorama:{photo}", "setb:panorama-enabled:1", "run", "configure:360x800",
               "signal:GameOrientation:(0.0, 0.0, 0.0, 1.0)", "state",
               "reply:ClaimGameOrientation:error", "signal:GameOrientation:(0.5, 0.0, 0.0, 0.5)",
               "signal:Orientation:(0.0, 0.0, 0.70710678, 0.70710678)", "state",
               "reply:ClaimGameOrientation:error", "appeared", "quit")
    out = r.stdout
    o = states(out, "orientation")
    # The game rotation vector first; after the bridge turned it down, the rotation vector (and
    # samples of the other one, claimed by Muga, are not ours)
    assert o[0] == "0.000,0.000,0.000,1.000"
    assert o[1] == "0.000,0.000,0.707,0.707"
    assert calls(out)[1:4] == ["ClaimGameOrientation", "ClaimOrientation", "ClaimOrientation"]
    assert states(out, "orientation_sensor") == ["GameOrientation", "Orientation"]
    assert kv(out)["effect"] == "panorama"
    assert "over the 360° photo" in r.stderr


def test_game_orientation_released_when_quitting(client, photo):
    out = client(f"set:panorama:{photo}", "setb:panorama-enabled:1", "run", "configure:360x800",
                 "reply:ClaimGameOrientation:ok", "quit").stdout
    assert lines(out, "dbus sync") == ["dbus sync net.hadess.SensorProxy ReleaseGameOrientation"]


def test_orientation_released_when_quitting_after_fallback(client, photo):
    out = client(f"set:panorama:{photo}", "setb:panorama-enabled:1", "run", "configure:360x800",
                 "reply:ClaimGameOrientation:error", "quit").stdout
    assert lines(out, "dbus sync") == ["dbus sync net.hadess.SensorProxy ReleaseOrientation"]


def test_orientation_released_with_the_sensor_in_use(client, photo):
    out = client(f"set:panorama:{photo}", "setb:panorama-enabled:1", "run", "configure:360x800",
                 "reply:ClaimGameOrientation:error", "set:panorama:", "set:effect:none", "state", "quit").stdout
    assert "ReleaseOrientation" in calls(out) and "ReleaseGameOrientation" not in calls(out)


def test_fixed_orientation_from_the_environment(client, photo):
    out = client(f"set:panorama:{photo}", "setb:panorama-enabled:1", "run", "state", "quit",
                 env={"PLINIUS_AMBIENT_ORIENTATION": "0,0,0.5,0.8660254", "FAKE_NO_BUS": "1"}).stdout
    assert kv(out)["has_orientation"] == "1" and kv(out)["orientation"] == "0.000,0.000,0.500,0.866"


# ---- The display ----

def test_display_state_from_mce(client, photo):
    out = client("set:effect:snow", "run", "configure:360x800", "reply:get_display_status:ok:('off',)", "state",
                 "signal:display_status_ind:('on',)", "state", "signal:display_status_ind:('on',)",
                 "signal:display_status_ind:(1,)", "state", "quit").stdout
    assert states(out, "display_on") == ["0", "1", "1"]


def test_no_mce(client):
    r = client("run", "reply:get_display_status:error", "state", "quit")
    assert kv(r.stdout)["display_on"] == "1"
    assert "No display state" in r.stdout + r.stderr


@pytest.fixture
def video(tmp_path):
    if not shutil.which("gst-launch-1.0"):
        pytest.skip("no gst-launch-1.0")
    v = tmp_path / "v.mkv"
    subprocess.run(["gst-launch-1.0", "-q", "videotestsrc", "num-buffers=30", "!",
                    "video/x-raw,format=I420,width=16,height=16,framerate=30/1", "!", "matroskamux", "!", "filesink",
                    f"location={v}"], check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin", "GST_REGISTRY": str(tmp_path / "registry.bin"), "HOME": str(tmp_path)})
    return v


def test_video_told_when_seen(client, video):
    out = client(f"set:video:{video}", "setb:video-enabled:1", "set:effect:snow", "run", "configure:360x800",
                 "state", "signal:display_status_ind:('off',)", "state", "signal:display_status_ind:('on',)",
                 "wait:1100", "state", "frame", "state", "quit").stdout
    # Seen, hidden with the display off, seen again; hidden when the callbacks stay away
    assert states(out, "told_visible") == ["1", "0", "0", "1"]
    assert kv(out)["effect"] == "snow"


# ---- Settings ----

def test_old_panorama_effect_becomes_the_background(client, photo):
    out = client(f"set:panorama:{photo}", "set:effect:panorama", "run", "state", "quit").stdout
    s = kv(out)
    # The photo under no effect, and the keys rewritten
    assert s["effect"] == "panorama" and s["picture"] == str(photo)


def test_density(client):
    out = client("set:effect:snow", "set:density:high", "run", "configure:360x800", "state",
                 "set:density:low", "state", "set:density:medium", "state", "quit").stdout
    assert states(out, "density") == ["2", "0", "1"]


def test_resolution_change_resizes(client):
    out = client("set:effect:snow", "run", "configure:360x800", "set:effect:balls", "state", "quit").stdout
    # Soft snow at half, sharp balls at the panel's pixels
    assert "wl egl window 540x1200" in out and "wl egl resize 1080x2400" in out
    assert kv(out)["buffer"] == "1080x2400"


def test_effect_changed_before_configure(client):
    out = client("set:effect:snow", "run", "set:effect:rain", "configure:360x800", "state", "quit").stdout
    assert kv(out)["effect"] == "rain" and len(lines(out, "wl create surface")) == 1


def test_video_and_photo_exclude_each_other(client, photo, video):
    out = client(f"set:panorama:{photo}", f"set:video:{video}", "setb:panorama-enabled:1", "setb:video-enabled:1",
                 "run", "state", "setb:panorama-enabled:1", "state", "setb:video-enabled:1", "state", "quit").stdout
    # Both on when it starts: the video wins; then the one switched on last
    assert states(out, "effect") == ["video", "panorama", "video"]


def test_new_background_picture_makes_them_step_aside(client, photo, video):
    out = client(f"set:panorama:{photo}", "setb:panorama-enabled:1", "run", "state",
                 "bg:primary-color:#ffffff", "state", "bg:picture-options:none", "state",
                 "bg:picture-uri:file:///x.png", "state", "quit").stdout
    # The colour and "no picture" leave the photo; a picture chosen takes its place
    assert states(out, "effect") == ["panorama", "panorama", "panorama", "none"]
    out = client(f"set:video:{video}", "setb:video-enabled:1", "set:effect:snow", "run", "state",
                 "bg:picture-options:spanned", "state", "quit").stdout
    # The snow stays, over the picture instead of the video
    assert states(out, "effect") == ["snow", "snow"] and states(out, "background") == ["1", "0"]


def test_without_the_background_schema(client):
    out = client("set:effect:snow", "run", "state", "quit", background=False).stdout
    assert kv(out)["effect"] == "snow"


def test_layer_closed(client):
    out = client("set:effect:snow", "run", "configure:360x800", "closed", "state", "quit").stdout
    s = kv(out)
    assert s["surface"] == "0" and s["configured"] == "0"
    assert lines(out, "wl live")[-1].startswith("wl live surface 0")


def test_output_change_redraws(client):
    out = client("set:effect:snow", "run", "configure:360x800", "frame", "output:0,720,1600,2", "state", "quit").stdout
    assert kv(out)["buffer"] == "360x800"


# ---- The compositor connection ----

def test_events_read_in_the_main_loop(client):
    out = client("run", "readable", "wait:30", "queued", "wait:30", "quit").stdout
    assert "wl read events" in out and "wl events queued" in out and "wl dispatch" in out
    assert kv(out)["exit"] == "0"


@pytest.mark.parametrize("how", ["hangup", "dispatch-error", "read-error"])
def test_lost_compositor(client, how):
    r = client("set:effect:snow", "run", "configure:360x800", how, "wait:2000", "state")
    assert "Lost the compositor" in r.stderr
    assert kv(r.stdout)["exit"] == "0"
