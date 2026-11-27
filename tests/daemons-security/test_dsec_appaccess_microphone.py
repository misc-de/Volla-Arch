# plinius-appaccess: the microphone per app in WirePlumber. plinius-appaccess-microphone.lua (the
# decisions) runs in a plain Lua 5.4 as WirePlumber has it; find-plinius-microphone-access.lua
# (the hook) runs on top of lua/dsec_wireplumber_fake.lua, the part of WirePlumber's Lua API it
# uses as fakes: permission managers, object managers of clients, nodes and links, Conf, State,
# ProcUtils, the file monitor and the idle loop. What reaches a real WirePlumber and PipeWire
# (the measurements on the phone) is in the hook's comment.

import json
import shutil
import subprocess

import pytest

from conftest import PKG
from dsec_helpers import HERE

APPACCESS = PKG / "plinius-appaccess"
FAKE = HERE / "lua" / "dsec_wireplumber_fake.lua"
HOOK = APPACCESS / "find-plinius-microphone-access.lua"
LUA = next((shutil.which(x) for x in ("lua5.4", "lua") if shutil.which(x)), None)

U = "/user.slice/user-32011.slice/user@32011.service"
SLICE = U + "/app.slice"
SNAP = SLICE + "/app-gnome-org.gnome.Snapshot-1234.scope"
RECORDER = SLICE + "/app-gnome-org.example.Recorder-77.scope"
BRIDGE = SLICE + "/audio-hal-bridge.service"
FILE = "/run/plinius-appaccess/microphone-32011.conf"
VERDICTS = {"default": "ask", "apps": {"org.example.Flat": "allow", "org.example.Denied": "deny"},
            "cgroups": {SNAP: "ask", RECORDER: "allow", BRIDGE: "service"}}

pytestmark = pytest.mark.skipif(LUA is None, reason="no Lua on the host")


def lua(chunk):
    """Runs @chunk after the decisions' module is loadable; its last line prints JSON"""
    prelude = (f'package.path = "{APPACCESS}/?.lua;" .. package.path\n'
               f'dofile ("{FAKE}")\n'
               'mic = require ("plinius-appaccess-microphone")\n')
    r = subprocess.run([LUA, "-"], input=prelude + chunk, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def lua_table(v):
    """A Python value as a Lua literal"""
    if isinstance(v, dict):
        return "{" + ", ".join(f"[{lua_table(k)}] = {lua_table(x)}" for k, x in v.items()) + "}"
    if isinstance(v, str):
        return json.dumps(v)
    return "nil" if v is None else str(v).lower()


# ---- The decisions ----

@pytest.mark.parametrize("props,cgroups,who", [
    # A native client: PipeWire's own pid of the socket
    ({"pipewire.sec.pid": "5000"}, {5000: f"0::{SNAP}"}, {"pid": 5000, "cgroup": SNAP}),
    # Through pipewire-pulse: its own pid is pipewire-pulse's, the app's is what libpulse says
    ({"client.api": "pipewire-pulse", "pipewire.sec.pid": "3776", "application.process.id": "5000"},
     {5000: f"0::{SNAP}", 3776: f"0::{U}/session.slice/pipewire-pulse.service"}, {"pid": 5000, "cgroup": SNAP}),
    # An app named by pipewire-pulse (Flatpak) or a portal: the app, never a pid
    ({"client.api": "pipewire-pulse", "pipewire.access.portal.app_id": "org.example.Flat",
      "application.process.id": "2"}, {}, {"app": "org.example.Flat"}),
    ({"pipewire.access.portal.app_id": "", "pipewire.sec.pid": "5000"}, {5000: SNAP},
     {"pid": 5000, "cgroup": SNAP}),
    ({"pipewire.sec.pid": "5000"}, {5000: "1:name=systemd:/x"}, {"pid": 5000}),
    ({"pipewire.sec.pid": "5000"}, {}, {"pid": 5000}),
    ({"client.api": "pipewire-pulse", "pipewire.sec.pid": "3776"}, {}, {}),
    ({"pipewire.sec.pid": "0"}, {}, {}),
    ({"pipewire.sec.pid": "abc"}, {}, {}),
    ({"pipewire.sec.pid": "1.5"}, {}, {}),
    ({}, {}, {}),
])
def test_who_a_client_is(props, cgroups, who):
    assert lua(f"W.cgroups = {lua_table(cgroups)}\n"
               f"print (W.json (mic.identify ({lua_table(props)}, function (pid) return W.cgroups[pid] end)))") == who


@pytest.mark.parametrize("who,verdict", [
    ({"app": "org.example.Flat"}, "allow"),
    ({"app": "org.example.Denied"}, "deny"),
    ({"app": "org.example.Unknown"}, "ask"),           # the default
    ({"pid": 5000, "cgroup": SNAP}, "ask"),
    ({"pid": 5000, "cgroup": RECORDER}, "allow"),
    ({"pid": 5000, "cgroup": BRIDGE}, "service"),
    # An app's own cgroups below its scope are the app's, however deep
    ({"pid": 5000, "cgroup": RECORDER + "/worker/deeper"}, "allow"),
    ({"pid": 5000, "cgroup": SNAP + "/x"}, "ask"),
    # Not seen by the daemon yet: refused until its file says
    ({"pid": 5000, "cgroup": SLICE + "/app-gnome-org.example.New-9.scope"}, "pending"),
    # Outside the apps: Phosh, the keyboard, PipeWire; no process; another kind of path
    ({"pid": 5000, "cgroup": U + "/session.slice/mobi.phosh.Shell.service"}, "allow"),
    ({"pid": 5000, "cgroup": "/user.slice/user-32011.slice/session-1.scope"}, "allow"),
    ({"pid": 5000, "cgroup": SLICE}, "allow"),
    ({"pid": 5000}, "allow"),
    ({}, "allow"),
])
def test_the_verdict_of_a_client(who, verdict):
    assert lua(f"print (W.json ({{mic.decide (mic.parse ({lua_table(VERDICTS)}), {lua_table(who)})}}))") == [verdict]


def test_without_verdicts_nothing_is_refused_or_counted():
    out = lua('local v = mic.decide (nil, { pid = 5000, cgroup = "' + SNAP + '" })\n'
              'print (W.json ({ v, mic.allowed (v), mic.report (v, {}) == nil }))')
    assert out == ["off", True, True]


@pytest.mark.parametrize("t,parsed", [
    (None, None),
    ("junk", None),
    ({}, {"default": "ask", "apps": {}, "cgroups": {}}),
    ({"default": 3, "apps": "x", "cgroups": {SNAP: "deny"}}, {"default": "ask", "apps": {}, "cgroups": {SNAP: "deny"}}),
    ({"default": "deny", "apps": {"a.b": "allow"}}, {"default": "deny", "apps": {"a.b": "allow"}, "cgroups": {}}),
])
def test_the_published_file_parsed(t, parsed):
    assert lua(f"print (W.json (mic.parse ({lua_table(t)}) or 'none'))") == (parsed or "none")


@pytest.mark.parametrize("verdict,props,report,allowed", [
    ("allow", {}, "used", True),
    ("service", {}, None, True),
    ("ask", {}, "refused", False),
    ("deny", {}, "refused", False),
    ("pending", {}, None, False),
    ("ask", {"stream.monitor": "true"}, None, False),        # a level meter
    ("ask", {"stream.capture.sink": "true"}, None, False),   # what plays
    ("ask", {"target.object": "hal_fm_input"}, None, False),  # the radio
])
def test_what_a_new_recording_reports(verdict, props, report, allowed):
    assert lua(f'print (W.json ({{ mic.report ("{verdict}", {lua_table(props)}) or "none",'
               f' mic.allowed ("{verdict}") }}))') == [report or "none", allowed]


def test_reports_keep_the_last_few_and_name_what_is_known():
    out = lua('local list, props = {}\n'
              'for i = 1, 5 do props = mic.report_add (list, tostring (i), "t" .. i, 3) end\n'
              'print (W.json ({ props, mic.report_text ("refused", { pid = 5000 }),'
              ' mic.report_text ("used", { app = "org.example.Flat" }) }))')
    assert out == [{"3": "t3", "4": "t4", "5": "t5"}, "refused 5000 -", "used 0 org.example.Flat"]


@pytest.mark.parametrize("dir,uid", [
    ("/run/user/32011", "32011"), ("/run/user/32011/", "32011"), ("/run/user/x", None), ("/tmp/r", None), (None, None),
])
def test_the_account_from_the_runtime_directory(dir, uid):
    assert lua(f"print (W.json ({{ mic.uid_of ({lua_table(dir)}) or 'none' }}))") == [uid or "none"]


# ---- The hook in WirePlumber ----

def hook(setup, actions, files=None, env=None, monitor=True, rescan=True):
    """The hook started in the fake world @setup made, then @actions; prints the last line"""
    files = {FILE: VERDICTS} if files is None else files
    env = {"XDG_RUNTIME_DIR": "/run/user/32011"} if env is None else env
    return lua(f"W.files = {lua_table(files)}\nW.env = {lua_table(env)}\n"
               + ('fm = W.plugin ("file-monitor-api")\n' if monitor else "")
               + ('W.plugin ("standard-event-source")\n' if rescan else "")
               + setup + f'\ndofile ("{HOOK}")\n' + actions)


WORLD = f'''
W.cgroups = {{ [5000] = "0::{SNAP}", [5001] = "0::{RECORDER}", [5002] = "0::{BRIDGE}",
              [5003] = "0::{SLICE}/app-gnome-org.example.New-9.scope" }}
snap = W.object ("clients", 40, {{ ["pipewire.sec.pid"] = "5000" }})
recorder = W.object ("clients", 41, {{ ["client.api"] = "pipewire-pulse", ["pipewire.sec.pid"] = "3776",
                                        ["application.process.id"] = "5001" }})
bridge = W.object ("clients", 42, {{ ["pipewire.sec.pid"] = "5002" }})
flat = W.object ("clients", 43, {{ ["client.api"] = "pipewire-pulse", ["pipewire.access.portal.app_id"] = "org.example.Flat" }})
new = W.object ("clients", 44, {{ ["pipewire.sec.pid"] = "5003" }})
mic_node = W.object ("nodes", 79, {{ ["media.class"] = "Audio/Source", ["node.name"] = "hal_input" }})
sink = W.object ("nodes", 81, {{ ["media.class"] = "Audio/Sink", ["node.name"] = "hal_output" }})
cam = W.object ("nodes", 139, {{ ["media.class"] = "Video/Source" }})
default_pm = W.pm (Perm.ALL)
flatpak_pm = W.pm (Perm.RX)
function stream (id, client, extra)
  local props = {{ ["media.class"] = "Stream/Input/Audio", ["client.id"] = tostring (client["bound-id"]) }}
  for k, v in pairs (extra or {{}}) do props[k] = v end
  return W.object ("nodes", id, props)
end
function perms (client, pm)
  return {{ mic = pm:permissions (client, mic_node), sink = pm:permissions (client, sink),
           cam = pm:permissions (client, cam) }}
end
'''


def test_the_rule_joins_the_chosen_permission_manager_once():
    out = hook(WORLD, '''
W.select_access (snap, default_pm)
W.select_access (bridge, default_pm)
W.select_access (flat, flatpak_pm)
W.select_access (new, nil)
print (W.json ({ #default_pm.matches, #flatpak_pm.matches, W.calls }))''')
    assert out == [1, 1, ["file-monitor-api add-watch /run/plinius-appaccess m"]]


def test_only_the_audio_sources_of_clients_that_may_not_record_are_taken():
    out = hook(WORLD, '''
W.select_access (snap, default_pm)
W.select_access (flat, flatpak_pm)
print (W.json ({ snap = perms (snap, default_pm), recorder = perms (recorder, default_pm),
                 bridge = perms (bridge, default_pm), new = perms (new, default_pm),
                 flat = perms (flat, flatpak_pm) }))''')
    assert out == {
        "snap": {"mic": 0, "sink": 510, "cam": 510},        # asks: no microphone, the rest as it was
        "recorder": {"mic": 510, "sink": 510, "cam": 510},  # allowed
        "bridge": {"mic": 510, "sink": 510, "cam": 510},    # a user service
        "new": {"mic": 0, "sink": 510, "cam": 510},         # not published yet
        "flat": {"mic": 500, "sink": 500, "cam": 500},      # allowed: never more than its manager gives
    }


def test_without_the_daemons_file_nothing_is_taken_or_reported():
    out = hook(WORLD, '''
W.select_access (snap, default_pm)
W.node_added (stream (150, snap))
print (W.json ({ perms (snap, default_pm).mic, W.saved }))''', files={})
    assert out == [510, {}]


def test_a_file_that_cannot_be_read_keeps_the_verdicts():
    out = hook(WORLD, '''
W.select_access (snap, default_pm)
W.files[FILE] = "unreadable"
fm.handlers.changed (fm, FILE, nil, "changed")
W.run_idle ()
print (W.json ({ perms (snap, default_pm).mic, W.log }))'''.replace("FILE", f'"{FILE}"'))
    assert out == [0, [f"W cannot read {FILE}: Permission denied"]]


def test_without_an_account_or_a_file_monitor():
    out = hook(WORLD, '''
W.select_access (snap, default_pm)
print (W.json ({ perms (snap, default_pm).mic, W.log }))''', env={"XDG_RUNTIME_DIR": "/tmp/x"})
    assert out == [510, ["W no account in XDG_RUNTIME_DIR: the microphone is not decided per app"]]
    out = hook(WORLD, '''
W.select_access (snap, default_pm)
print (W.json ({ perms (snap, default_pm).mic, W.log }))''', monitor=False)
    assert out == [0, ["W no file-monitor-api: the microphone's verdicts are read only at start"]]


def test_new_recordings_are_reported_once_and_level_meters_not_at_all():
    out = hook(WORLD, '''
W.node_added (stream (150, snap))
W.node_added (W.nodes[#W.nodes])
W.node_added (stream (151, recorder))
W.node_added (stream (152, bridge))
W.node_added (stream (153, snap, { ["stream.monitor"] = "true" }))
W.node_added (stream (154, flat))
W.node_added (stream (155, { ["bound-id"] = 99 }))
local saved = W.saved["plinius-appaccess-microphone"]
local list = {}
for k, v in pairs (saved) do table.insert (list, v) end
table.sort (list)
print (W.json (list))''')
    assert out == ["refused 5000 -", "used 0 org.example.Flat", "used 5001 -"]


def test_only_the_last_eight_reports_stay():
    out = hook(WORLD, '''
for i = 1, 10 do W.node_added (stream (200 + i, snap, { ["object.serial"] = tostring (1000 + i) })) end
local keys = {}
for k in pairs (W.saved["plinius-appaccess-microphone"]) do table.insert (keys, k) end
table.sort (keys)
print (W.json (keys))''')
    assert out == [str(1000 + i) for i in range(3, 11)]


NEW = SLICE + "/app-gnome-org.example.New-9.scope"


def test_a_new_app_waits_for_its_verdict_and_gets_it_with_the_next_file():
    out = hook(WORLD, f'''
W.select_access (new, default_pm)
W.node_added (stream (150, new))
local before = {{ perms (new, default_pm).mic, W.saved["plinius-appaccess-microphone"] or "none" }}
W.files["{FILE}"].cgroups["{NEW}"] = "ask"
fm.handlers.changed (fm, "{FILE}.new", "{FILE}", "renamed")
fm.handlers.changed (fm, "{FILE}", nil, "changes-done-hint")
fm.handlers.changed (fm, "/run/plinius-appaccess/microphone-1.conf", nil, "created")
local waiting = #W.idle
W.run_idle ()
local texts = {{}}
for _, v in pairs (W.saved["plinius-appaccess-microphone"]) do table.insert (texts, v) end
print (W.json ({{ before, waiting, default_pm.updates, texts, W.calls }}))''')
    assert out == [[0, "none"], 1, 1, ["refused 5003 -"],
                   ["file-monitor-api add-watch /run/plinius-appaccess m",
                    "standard-event-source schedule-rescan linking"]]


def test_an_allowed_app_gets_the_sources_and_its_waiting_stream_is_linked():
    out = hook(WORLD, f'''
W.select_access (snap, default_pm)
local before = perms (snap, default_pm).mic
W.files["{FILE}"].cgroups["{SNAP}"] = "allow"
fm.handlers.changed (fm, "{FILE}", nil, "created")
W.run_idle ()
print (W.json ({{ before, perms (snap, default_pm).mic, W.calls[2] }}))''')
    assert out == [0, 510, "standard-event-source schedule-rescan linking"]


def test_a_recording_that_loses_the_microphone_is_ended():
    out = hook(WORLD + f'''
s1 = stream (150, recorder)
s2 = stream (151, bridge)
m1 = W.object ("nodes", 152, {{ ["media.class"] = "Stream/Input/Audio", ["client.id"] = "41",
                                ["stream.capture.sink"] = "true" }})
W.object ("links", 300, {{ ["link.output.node"] = "79", ["link.input.node"] = "150" }})
W.object ("links", 301, {{ ["link.output.node"] = "79", ["link.input.node"] = "151" }})
W.object ("links", 302, {{ ["link.output.node"] = "81", ["link.input.node"] = "152" }})
W.object ("links", 303, {{ ["link.output.node"] = "79", ["link.input.node"] = "999" }})
''', f'''
W.select_access (recorder, default_pm)
W.files["{FILE}"].cgroups["{RECORDER}"] = "deny"
fm.handlers.changed (fm, "{FILE}", nil, "created")
W.run_idle ()
print (W.json ({{ W.destroyed, perms (recorder, default_pm).mic }}))''', rescan=False)
    # Only the microphone's link of the app that lost it; a recording of what plays stays
    assert out == [[300], 0]


def test_the_radio_is_no_microphone():
    out = hook(WORLD + f'''
fm_node = W.object ("nodes", 80, {{ ["media.class"] = "Audio/Source", ["node.name"] = "hal_fm_input" }})
s1 = stream (150, recorder)
W.object ("links", 300, {{ ["link.output.node"] = "79", ["link.input.node"] = "150" }})
W.object ("links", 301, {{ ["link.output.node"] = "80", ["link.input.node"] = "150" }})
''', f'''
W.select_access (snap, default_pm)
W.select_access (flat, flatpak_pm)
W.select_access (recorder, default_pm)
W.node_added (stream (151, snap, {{ ["target.object"] = "hal_fm_input" }}))
W.files["{FILE}"].cgroups["{RECORDER}"] = "deny"
fm.handlers.changed (fm, "{FILE}", nil, "created")
W.run_idle ()
print (W.json ({{ snap = {{ default_pm:permissions (snap, fm_node), default_pm:permissions (snap, mic_node) }},
                 flat = flatpak_pm:permissions (flat, fm_node),
                 recorder = default_pm:permissions (recorder, fm_node),
                 destroyed = W.destroyed, saved = W.saved }}))''', rescan=False)
    # Every app sees the tuner, listening is no recording (only the microphone's recording of the
    # app that lost it is reported), and losing the microphone ends only the microphone's link
    assert out == {"snap": [510, 0], "flat": 500, "recorder": 510, "destroyed": [300],
                   "saved": {"plinius-appaccess-microphone": {"110": "refused 5001 -"}}}


def test_a_client_whose_properties_change_is_decided_anew():
    out = hook(WORLD + '''
late = W.object ("clients", 45, { ["client.api"] = "pipewire-pulse", ["pipewire.sec.pid"] = "3776" })
''', '''
W.select_access (late, default_pm)
local before = perms (late, default_pm).mic
default_pm.handlers["client-properties-changed"] (default_pm, late)
local same = default_pm.updates
late.properties["application.process.id"] = "5000"
default_pm.handlers["client-properties-changed"] (default_pm, late)
local after = default_pm.updates
default_pm.handlers["client-properties-changed"] (default_pm, W.object ("clients", 46, {}))
print (W.json ({ before, same, after, perms (late, default_pm).mic, default_pm.updates }))''')
    assert out == [510, 0, 1, 0, 1]


def test_a_client_or_stream_that_is_gone_is_forgotten():
    out = hook(WORLD, f'''
W.select_access (snap, default_pm)
local before = perms (snap, default_pm).mic
W.cgroups[5000] = "0::{RECORDER}"
local cached = perms (snap, default_pm).mic
W.remove ("clients", snap)
table.insert (W.clients, snap)
local fresh = perms (snap, default_pm).mic
local s = stream (150, recorder)
W.node_added (s)
W.remove ("nodes", s)
table.insert (W.nodes, s)
W.node_added (s)
W.remove ("clients", W.object ("clients", 47, {{}}))
print (W.json ({{ before, cached, fresh, #report_list }}))''')
    # The process's cgroup is read once per client; a stream gone and come again is new
    assert out == [0, 0, 510, 2]
