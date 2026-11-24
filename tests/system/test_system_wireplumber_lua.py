# The WirePlumber Lua scripts of the phone, run by the host's lua against the stand-ins in
# wp_mock.lua: new audio devices become the default, camera nodes only for the phone's own
# parts, alerts never in the earpiece, hands-free streams to the HAL.

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import PKG

MOCK = Path(__file__).with_name("wp_mock.lua")
NEW_DEVICE = PKG / "device-volla-ansuz" / "plinius-new-device-default.lua"
CAMERA = PKG / "camera-hal-bridge" / "find-plinius-camera-access.lua"
ALERTS = PKG / "audio-hal-bridge" / "alerts-to-speaker.lua"
SCO = PKG / "audio-hal-bridge" / "sco-to-hal.lua"

LUA = shutil.which("lua5.4") or shutil.which("lua")


def lua(script, scenario):
    if LUA is None:
        pytest.skip("no lua on the host")
    code = f'dofile("{MOCK}")\ndofile("{script}")\n{scenario}\n'
    r = subprocess.run([LUA, "-"], input=code, capture_output=True, text=True, timeout=10,
                       env={"PATH": "/usr/bin:/bin", "LUA_PATH": "", "LUA_CPATH": ""})
    assert r.returncode == 0, r.stderr
    return r.stdout


# --- plinius-new-device-default.lua ---

SETUP = '''
local SPEAKER = '{"name":"hal_output"}'
meta = Metadata { ["default.configured.audio.sink"] = SPEAKER }
devices = ObjectManager {
  { ["bound-id"] = 50, properties = { ["device.bus"] = "bluetooth", ["device.name"] = "bluez_card.X" } },
  { ["bound-id"] = 60, properties = { ["device.bus"] = "usb", ["device.name"] = "alsa_card.usb" } },
  { ["bound-id"] = 70, properties = { ["device.bus"] = "pci", ["device.name"] = "alsa_card.pci" } },
}
source = Source { metadata = ObjectManager { meta }, device = devices }
function added (props)
  local si = SessionItem (Node (props))
  run_hook ("plinius/new-device-default", Event (source, si))
end
function removed (id)
  run_hook ("plinius/new-device-default-forget", Event (source, { ["bound-id"] = id }))
end
function sink () return meta.values ["default.configured.audio.sink"] end
function source_default () return meta.values ["default.configured.audio.source"] end
'''


def test_new_device_hook_interests():
    out = lua(NEW_DEVICE, '''
local i = HOOKS ["plinius/new-device-default"].interests [1]
print (i [1][3], i [2][3], i [3][2], i [3][3], i [3][4])
print (HOOKS ["plinius/new-device-default-forget"].interests [1][1][3])''')
    assert out.splitlines() == ["session-item-added\tlinkable\tc\tAudio/Sink\tAudio/Source",
                                "device-removed"]


def test_bluetooth_sink_becomes_default():
    out = lua(NEW_DEVICE, SETUP + '''
added { ["node.name"] = "bluez_output.X.1", ["media.class"] = "Audio/Sink", ["device.id"] = "50" }
print (sink ())
print (#meta.sets, meta.sets [1].type)''')
    assert out.splitlines() == ['{"name":"bluez_output.X.1"}', "1\tSpa:String:JSON"]


def test_bluetooth_microphone_of_bluez_is_not_taken():
    # SCO audio never crosses HCI here: BlueZ's source records silence
    out = lua(NEW_DEVICE, SETUP + '''
added { ["node.name"] = "bluez_input.X", ["media.class"] = "Audio/Source", ["device.id"] = "50" }
print (tostring (source_default ()), #meta.sets)''')
    assert out.split() == ["nil", "0"]


def test_headset_microphone_of_the_bridge_becomes_default():
    out = lua(NEW_DEVICE, SETUP + '''
local p = { ["node.name"] = "hal_headset_input", ["media.class"] = "Audio/Source",
            ["audio-hal-bridge.headset"] = "true" }
added (p)
added (p)  -- already the default: no second write
print (source_default (), #meta.sets)''')
    assert out.split() == ['{"name":"hal_headset_input"}', "1"]


def test_usb_sink_and_source_become_default():
    out = lua(NEW_DEVICE, SETUP + '''
added { ["node.name"] = "usb_out", ["media.class"] = "Audio/Sink", ["device.id"] = "60" }
added { ["node.name"] = "usb_in", ["media.class"] = "Audio/Source", ["device.id"] = "60" }
print (sink (), source_default ())''')
    assert out.split() == ['{"name":"usb_out"}', '{"name":"usb_in"}']


@pytest.mark.parametrize("props", [
    # A bus that does not come and go (the vendor HAL nodes have no device at all)
    '{ ["node.name"] = "pci_out", ["media.class"] = "Audio/Sink", ["device.id"] = "70" }',
    '{ ["node.name"] = "hal_output", ["media.class"] = "Audio/Sink" }',
    # Unknown device id, missing name, other media class
    '{ ["node.name"] = "x", ["media.class"] = "Audio/Sink", ["device.id"] = "99" }',
    '{ ["media.class"] = "Audio/Sink", ["device.id"] = "50" }',
    '{ ["node.name"] = "v", ["media.class"] = "Video/Source", ["device.id"] = "60" }',
])
def test_other_nodes_change_nothing(props):
    out = lua(NEW_DEVICE, SETUP + f"added ({props})\nprint (#meta.sets)")
    assert out.strip() == "0"


def test_profile_change_follows_only_if_the_user_kept_it():
    out = lua(NEW_DEVICE, SETUP + '''
added { ["node.name"] = "a2dp", ["media.class"] = "Audio/Sink", ["device.id"] = "50" }
-- A2DP -> headset profile: the new node takes over
added { ["node.name"] = "hfp", ["media.class"] = "Audio/Sink", ["device.id"] = "50" }
print (sink ())
-- The user picks the speaker; a later profile change keeps that choice
meta.values ["default.configured.audio.sink"] = '{"name":"hal_output"}'
added { ["node.name"] = "a2dp", ["media.class"] = "Audio/Sink", ["device.id"] = "50" }
print (sink ())
-- After disconnecting, the next connection wins again
removed (50)
added { ["node.name"] = "a2dp", ["media.class"] = "Audio/Sink", ["device.id"] = "50" }
print (sink ())''')
    assert out.split() == ['{"name":"hfp"}', '{"name":"hal_output"}', '{"name":"a2dp"}']


def test_without_default_metadata_nothing_happens():
    out = lua(NEW_DEVICE, SETUP + '''
source.oms.metadata = ObjectManager {}
added { ["node.name"] = "x", ["media.class"] = "Audio/Sink", ["device.id"] = "50" }
print ("ok")''')
    assert out.strip() == "ok"


# --- find-plinius-camera-access.lua ---

CAMERA_SETUP = '''
pm = PERMISSION_MANAGERS [1]
function perm (binary)
  return pm.matches [1].fn (pm, Client { ["application.process.binary"] = binary }, nil)
end
function select (access, data)
  local e = Event (nil, Client { ["pipewire.access"] = access }, data)
  run_hook ("client/find-plinius-camera-access", e)
  return e.data ["permission-manager"] == pm
end
'''


def test_camera_permissions_per_binary():
    out = lua(CAMERA, CAMERA_SETUP + '''
for _, b in ipairs { "camera-hal-bridge", "plinius-usb-webcam", "xdg-desktop-portal",
                     "wireplumber", "pipewire", "phosh", "firefox", "ffmpeg", "" } do
  print (b, perm (b))
end
print (pm.matches [1].fn (pm, Client {}, nil))''')
    assert dict(line.split("\t") for line in out.splitlines()[:-1]) == {
        "camera-hal-bridge": "rwxm", "plinius-usb-webcam": "rwxm", "xdg-desktop-portal": "rwxm",
        "wireplumber": "rwxm", "pipewire": "rwxm", "phosh": "rx", "firefox": "-", "ffmpeg": "-",
        "": "-"}
    assert out.splitlines()[-1] == "-"


def test_camera_interest_only_camera_nodes_and_activated():
    out = lua(CAMERA, CAMERA_SETUP + '''
local i = pm.matches [1].interest
print (i.type, i [1][1], i [1][3], i [2][1], i [2][3], pm.default, tostring (pm.active))
local h = HOOKS ["client/find-plinius-camera-access"]
print (h.before, h.after [1], h.after [2])''')
    assert out.splitlines() == [
        "node\tmedia.role\tCamera\tmedia.class\tVideo/Source\trwxm\ttrue",
        "client/find-default-access\tclient/find-config-access\tclient/find-portal-access"]


def test_camera_manager_only_for_native_clients():
    out = lua(CAMERA, CAMERA_SETUP + '''
print (select (nil), select ("unrestricted"), select ("default"))
print (select ("flatpak"), select ("restricted"), select ("portal"))
-- A permission manager chosen earlier (portal, config rule) stays
local other = {}
local e = Event (nil, Client { ["pipewire.access"] = "unrestricted" }, { ["permission-manager"] = other })
run_hook ("client/find-plinius-camera-access", e)
print (e.data ["permission-manager"] == other)''')
    assert out.split() == ["true", "true", "true", "false", "false", "false", "true"]


# --- alerts-to-speaker.lua, sco-to-hal.lua (select-target hooks) ---

LINK_SETUP = '''
speaker = SessionItem (Node { ["node.name"] = "hal_output", ["media.class"] = "Audio/Sink" })
earpiece = SessionItem (Node { ["node.name"] = "hal_earpiece", ["media.class"] = "Audio/Sink" })
hal_in = SessionItem (Node { ["node.name"] = "hal_input", ["media.class"] = "Audio/Source" })
hs_in = SessionItem (Node { ["node.name"] = "hal_headset_input", ["media.class"] = "Audio/Source" })
function om_with (...)
  local objs = {}
  for _, si in ipairs { ... } do table.insert (objs, { properties = si.node.properties, si = si }) end
  local om = ObjectManager (objs)
  local lookup = om.lookup
  function om:lookup (i) local o = lookup (self, i); return o and o.si end
  return om
end
function select_target (hook, stream_props, target, om)
  local e = Event (nil, SessionItem (nil, stream_props), { target = target, om = om })
  run_hook (hook, e)
  local t = e.data.target
  return t and t.node.properties ["node.name"] or "none"
end
'''


@pytest.mark.parametrize("props,moved", [
    ('{ ["media.role"] = "Ringtone" }', True),
    ('{ ["media.role"] = "alarm" }', True),
    ('{ ["media.role"] = "Notification" }', True),
    ('{ ["node.name"] = "output.loopback.sink.role.ringtone" }', True),
    ('{ ["application.process.binary"] = "feedbackd" }', True),
    ('{ ["media.role"] = "Music" }', False),
    ('{ ["media.role"] = "Phone" }', False),
    ('{ ["node.name"] = "output.loopback.sink.role.multimedia" }', False),
    ("{}", False),
])
def test_alerts_leave_the_earpiece(props, moved):
    out = lua(ALERTS, LINK_SETUP + f'''
print (select_target ("linking/audio-hal-bridge-alerts", {props}, earpiece, om_with (speaker, earpiece)))''')
    assert out.strip() == ("hal_output" if moved else "hal_earpiece")


def test_alerts_to_other_targets_stay():
    out = lua(ALERTS, LINK_SETUP + '''
print (select_target ("linking/audio-hal-bridge-alerts", { ["media.role"] = "Ringtone" },
                      hs_in, om_with (speaker)))
print (select_target ("linking/audio-hal-bridge-alerts", { ["media.role"] = "Ringtone" },
                      nil, om_with (speaker)))
-- No speaker, or it cannot be linked: the earpiece stays rather than nothing
print (select_target ("linking/audio-hal-bridge-alerts", { ["media.role"] = "Ringtone" },
                      earpiece, om_with (earpiece)))
CAN_LINK = false
print (select_target ("linking/audio-hal-bridge-alerts", { ["media.role"] = "Ringtone" },
                      earpiece, om_with (speaker, earpiece)))''')
    assert out.split() == ["hal_headset_input", "none", "hal_earpiece", "hal_earpiece"]


HFP_SETUP = LINK_SETUP + '''
hfp_sink = SessionItem (Node { ["node.name"] = "bluez_output.X.headset-head-unit",
  ["media.class"] = "Audio/Sink", ["api.bluez5.profile"] = "headset-head-unit" })
hfp_src = SessionItem (Node { ["node.name"] = "bluez_input.X.headset-head-unit-msbc",
  ["media.class"] = "Audio/Source", ["api.bluez5.profile"] = "headset-head-unit-msbc" })
a2dp = SessionItem (Node { ["node.name"] = "bluez_output.X.a2dp-sink",
  ["media.class"] = "Audio/Sink", ["api.bluez5.profile"] = "a2dp-sink" })
H = "linking/audio-hal-bridge-sco"
'''


def test_sco_streams_go_to_the_hal():
    out = lua(SCO, HFP_SETUP + '''
local om = om_with (speaker, hal_in, hs_in)
print (select_target (H, { ["node.name"] = "firefox" }, hfp_sink, om))
print (select_target (H, { ["node.name"] = "calls" }, hfp_src, om))
-- Without the bridge's headset source the phone's microphone
print (select_target (H, { ["node.name"] = "calls" }, hfp_src, om_with (speaker, hal_in)))
-- The role sinks' output streams carry all playback
print (select_target (H, { ["node.link-group"] = "g", ["media.role"] = "Loopback" }, hfp_sink, om))''')
    assert out.split() == ["hal_output", "hal_headset_input", "hal_input", "hal_output"]


def test_sco_leaves_hold_filters_and_other_profiles():
    out = lua(SCO, HFP_SETUP + '''
local om = om_with (speaker, hal_in, hs_in)
print (select_target (H, { ["node.name"] = "audio-hal-bridge.sco-hold" }, hfp_sink, om))
print (select_target (H, { ["node.link-group"] = "loopback-1" }, hfp_src, om))
print (select_target (H, { ["node.name"] = "music" }, a2dp, om))
print (select_target (H, { ["node.name"] = "music" }, speaker, om))
print (select_target (H, { ["node.name"] = "music" }, hfp_sink, om_with (hal_in)))
CAN_LINK = false
print (select_target (H, { ["node.name"] = "music" }, hfp_sink, om))''')
    assert out.split() == [
        "bluez_output.X.headset-head-unit", "bluez_input.X.headset-head-unit-msbc",
        "bluez_output.X.a2dp-sink", "hal_output", "bluez_output.X.headset-head-unit",
        "bluez_output.X.headset-head-unit"]
