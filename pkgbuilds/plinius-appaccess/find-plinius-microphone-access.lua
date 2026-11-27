-- Volla Phone Plinius (plinius-appaccess): the microphone per app, as plinius-appaccessd decides.
-- SPDX-License-Identifier: MIT
--
-- A client of an app whose verdict is not "allow" gets no permissions on the audio sources (hal_input
-- of audio-hal-bridge, a headset's microphone): it does not see them, and PipeWire links no stream
-- of it to one (measured on the phone, WirePlumber 0.5.19: a link of a client without permissions
-- on the source fails). The verdicts come from the daemon's file for this account
-- (plinius-appaccess-microphone.lua says how a client is matched to its app); without the file
-- (the daemon is not running) nothing is refused. When the file changes, every client is decided
-- anew: what is allowed now gets the sources, and the streams waiting for them are linked
-- (WirePlumber links again only on a rescan); a recording of a client that lost the sources is
-- ended (its link destroyed: taking the permissions back does not stop a running stream).
--
-- A client gets one permission manager (the camera's, the default one, Flatpak's …): this hook
-- adds the microphone's rule to the one chosen, after every other script of the "select-access"
-- event and before apply-access attaches it.
--
-- The FM tuner's source (hal_fm_input of audio-hal-bridge) is no microphone: it carries a broadcast,
-- never the room, so every app sees it, its recordings are not reported and not ended (a radio
-- app finds out at start whether the source is there, without recording).
--
-- Each new capture stream is reported to plinius-appaccess-agent through the state file
-- plinius-appaccess-microphone (~/.local/state/wireplumber): refused ones bring its question,
-- allowed ones are counted as uses. Not reported: level meters and recordings of what plays.

mic = require ("plinius-appaccess-microphone")
log = Log.open_topic ("s-client")

uid = mic.uid_of (os.getenv ("XDG_RUNTIME_DIR"))
path = uid and ("/run/plinius-appaccess/microphone-" .. uid .. ".conf") or nil
SOURCES = Interest { type = "node", Constraint { "media.class", "#", "Audio/Source*" },
                     Constraint { "node.name", "!", mic.FM_SOURCE } }
CAPTURE = "Stream/Input/Audio*"
-- So many reports stay in the state file: the agent reads the new ones
REPORTS_KEPT = 8

state = nil
managers = {}      -- tostring (pm) -> pm: the permission managers with the microphone's rule
clients = {}       -- client serial -> { who, verdict }
streams = {}       -- capture stream serial -> true reported (or nothing to report), false pending
report_list = {}
reports = State ("plinius-appaccess-microphone")
pending_update = false

function load ()
  if path == nil then
    state = nil
    return
  end
  local conf = Conf (path, { ["as-section"] = "plinius.microphone", ["no-fragments"] = "true" })
  local err = conf:open ()
  if err == nil then
    state = mic.parse (conf:get_section_as_object ("plinius.microphone"))
  elseif err:find ("Could not locate", 1, true) then
    state = nil
  else
    -- Half a change cannot be seen (the daemon renames), so this is the file's mode: keep what was
    log:warning ("cannot read " .. path .. ": " .. err)
  end
end

function cgroup_of (pid)
  local info = ProcUtils.get_proc_info (pid)
  return info and info:get_cgroup ()
end

-- The client's app (from its process) and verdict; the process is looked at once per client
function decide (client)
  local serial = client.properties["object.serial"]
  local c = serial and clients[serial]

  if c == nil then
    c = { who = mic.identify (client.properties, cgroup_of) }
    if serial then
      clients[serial] = c
    end
  end
  c.verdict = mic.decide (state, c.who)
  return c
end

function extend (pm)
  local key = tostring (pm)

  if managers[key] then
    return
  end
  managers[key] = pm
  pm:add_interest_match (function (m, client, _)
    local c = decide (client)
    if mic.allowed (c.verdict) then
      return m:get_default_permissions ()
    end
    return Perm.NONE
  end, SOURCES)
  -- A client's properties may change (pipewire-pulse): decided anew
  pm:connect ("client-properties-changed", function (m, client)
    local serial = client.properties["object.serial"]
    local old = serial and clients[serial]
    if old == nil then
      return
    end
    clients[serial] = nil
    if not mic.allowed (old.verdict) ~= not mic.allowed (decide (client).verdict) then
      m:update_permissions ()
    end
  end)
end

clients_om = ObjectManager { Interest { type = "client" } }
nodes_om = ObjectManager { Interest { type = "node" } }
links_om = ObjectManager { Interest { type = "link" } }

clients_om:connect ("object-removed", function (_, client)
  local serial = client.properties["object.serial"]
  if serial then
    clients[serial] = nil
  end
end)
nodes_om:connect ("object-removed", function (_, node)
  local serial = node.properties["object.serial"]
  if serial then
    streams[serial] = nil
  end
end)

function by_id (om, id)
  id = tonumber (id)
  return id and om:lookup { Constraint { "bound-id", "=", id, type = "gobject" } }
end

-- A new capture stream: reported once its client's verdict is known
function consider (node)
  local serial = node.properties["object.serial"]
  local client = by_id (clients_om, node.properties["client.id"])

  if serial == nil or streams[serial] == true or client == nil then
    return
  end
  local c = decide (client)
  if c.verdict == "pending" then
    streams[serial] = false
    return
  end
  streams[serial] = true
  local kind = mic.report (c.verdict, node.properties)
  if kind then
    log:info (client, string.format ("microphone %s for client %s", kind, tostring (client["bound-id"])))
    reports:save (mic.report_add (report_list, serial, mic.report_text (kind, c.who), REPORTS_KEPT))
  end
end

-- Recordings of clients that lost the sources: their links end
function revoke ()
  for link in links_om:iterate () do
    local out = by_id (nodes_om, link.properties["link.output.node"])
    local input = by_id (nodes_om, link.properties["link.input.node"])
    local client = input and by_id (clients_om, input.properties["client.id"])

    if out and client and (out.properties["media.class"] or ""):find ("^Audio/Source") and
        out.properties["node.name"] ~= mic.FM_SOURCE and not mic.allowed (decide (client).verdict) then
      log:info (client, "microphone taken back: link " .. tostring (link["bound-id"]) .. " destroyed")
      link:request_destroy ()
    end
  end
end

function update ()
  pending_update = false
  load ()
  for _, pm in pairs (managers) do
    pm:update_permissions ()
  end
  revoke ()
  for node in nodes_om:iterate { Constraint { "media.class", "#", CAPTURE } } do
    consider (node)
  end
  -- Streams that waited for a source now allowed to them
  local source = Plugin.find ("standard-event-source")
  if source then
    source:call ("schedule-rescan", "linking")
  end
  return false
end

SimpleEventHook {
  name = "client/find-plinius-microphone-access",
  after = { "client/find-config-access", "client/find-portal-access", "client/find-flatpak-access",
            "client/find-snap-access", "client/find-default-access", "client/find-plinius-camera-access" },
  before = "client/apply-access",
  interests = {
    EventInterest {
      Constraint { "event.type", "=", "select-access" },
    },
  },
  execute = function (event)
    local pm = event:get_data ("permission-manager")
    if pm ~= nil then
      extend (pm)
    end
  end
}:register ()

SimpleEventHook {
  name = "node/plinius-microphone-report",
  interests = {
    EventInterest {
      Constraint { "event.type", "=", "node-added" },
      Constraint { "media.class", "#", CAPTURE, type = "pw-global" },
    },
  },
  execute = function (event)
    consider (event:get_subject ())
  end
}:register ()

clients_om:activate ()
nodes_om:activate ()
links_om:activate ()
load ()

if path == nil then
  log:warning ("no account in XDG_RUNTIME_DIR: the microphone is not decided per app")
else
  fm = Plugin.find ("file-monitor-api")
  if fm == nil then
    log:warning ("no file-monitor-api: the microphone's verdicts are read only at start")
  else
    -- The directory: the daemon replaces the file by a rename
    fm:connect ("changed", function (_, file, other, _)
      if (file == path or other == path) and not pending_update then
        pending_update = true
        Core.idle_add (update)
      end
    end)
    fm:call ("add-watch", "/run/plinius-appaccess", "m")
  end
end
