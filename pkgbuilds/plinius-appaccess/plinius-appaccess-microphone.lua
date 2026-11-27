-- Volla Phone Plinius (plinius-appaccess): what WirePlumber's hook find-plinius-microphone-access.lua
-- decides, without WirePlumber (tests/daemons-security runs it in a plain Lua).
-- SPDX-License-Identifier: MIT
--
-- plinius-appaccessd publishes the microphone's verdicts of an account in
-- /run/plinius-appaccess/microphone-<uid>.conf: "cgroups" (every cgroup of the account below
-- user@<uid>.service/app.slice: "allow", "ask", "deny" or "service"), "apps" (every app it knows)
-- and "default" (an app it does not know). A client of PipeWire is one of an app by the process
-- it runs in, the process by its cgroup:
-- - a native client: pipewire.sec.pid (PipeWire took it from the socket);
-- - a client of pipewire-pulse: its pipewire.sec.pid is pipewire-pulse's own, the app's process
--   is application.process.id, which the app gives itself (as the name of its scope: a native
--   app is trusted to say who it is, plinius-appaccessd.c, "Limits");
-- - a client that names its app (pipewire.access.portal.app_id, set by pipewire-pulse for a
--   Flatpak app and by the portals, never by the app): that app, also when its process lives in
--   another pid namespace.

local M = {}

-- user.slice/user-<uid>.slice/user@<uid>.service/app.slice/…: where the apps of a session run
M.APP_SLICE = "^/user%.slice/user%-%d+%.slice/user@%d+%.service/app%.slice/"

-- The FM tuner's source (audio-hal-bridge): a broadcast, no microphone
M.FM_SOURCE = "hal_fm_input"

-- The table of the published file (Conf's section), nil for none: then nothing is refused (the
-- daemon is not running, as for its other resources)
function M.parse (t)
  if type (t) ~= "table" then
    return nil
  end
  return {
    default = type (t.default) == "string" and t.default or "ask",
    apps = type (t.apps) == "table" and t.apps or {},
    cgroups = type (t.cgroups) == "table" and t.cgroups or {},
  }
end

-- The path in the first line of /proc/<pid>/cgroup ("0::/user.slice/…"), nil without
function M.cgroup_path (line)
  if type (line) ~= "string" then
    return nil
  end
  return line:match ("^0::(/.*)$") or line:match ("^(/.*)$")
end

-- Who the client with properties @props is: { app = id }, { pid = n, cgroup = path } or {}
-- (nothing that tells); @cgroup_of (pid) gives the first line of the process's cgroup file
function M.identify (props, cgroup_of)
  local app = props["pipewire.access.portal.app_id"]
  local pid

  if app ~= nil and app ~= "" then
    return { app = app }
  end
  if props["client.api"] == "pipewire-pulse" then
    pid = tonumber (props["application.process.id"])
  else
    pid = tonumber (props["pipewire.sec.pid"])
  end
  pid = pid and math.tointeger (pid)
  if pid == nil or pid <= 0 then
    return {}
  end
  return { pid = pid, cgroup = M.cgroup_path (cgroup_of (pid)) }
end

-- The verdict for a client @who: "allow", "ask", "deny", "service" (a user service: allowed),
-- "pending" (an app's cgroup the daemon has not published yet: refused until it has) or "off"
-- (no verdicts: the daemon is not running, nothing is refused or counted)
function M.decide (state, who)
  local path = who.cgroup

  if state == nil then
    return "off"
  end
  if who.app then
    return state.apps[who.app] or state.default
  end
  -- No process, or one outside the apps: Phosh, the keyboard, the portals, PipeWire itself …
  if path == nil or not path:match (M.APP_SLICE) then
    return "allow"
  end
  -- The cgroup or the nearest one above it that the daemon has (an app's own cgroups below its
  -- scope are the app's; a name that is no UTF-8 is left out of the file)
  while path:match (M.APP_SLICE) do
    local verdict = state.cgroups[path]

    if verdict ~= nil then
      return verdict
    end
    path = path:match ("^(.*)/[^/]*$")
  end
  return "pending"
end

-- Whether a client with @verdict may see the audio sources
function M.allowed (verdict)
  return verdict == "allow" or verdict == "service" or verdict == "off"
end

-- What to tell the agent about a new capture stream (properties @props) of a client with
-- @verdict: "used", "refused" or nil (a service, a verdict still pending, no verdicts, a level
-- meter, a recording of what plays, the radio: target.object is the source a Pulse client asked
-- for by name)
function M.report (verdict, props)
  if props["stream.monitor"] == "true" or props["stream.capture.sink"] == "true" or
      props["target.object"] == M.FM_SOURCE then
    return nil
  end
  if verdict == "allow" then
    return "used"
  end
  if verdict == "ask" or verdict == "deny" then
    return "refused"
  end
  return nil
end

-- One report as the agent reads it: "<kind> <pid> <app>", 0 and "-" for what is not known
function M.report_text (kind, who)
  return string.format ("%s %d %s", kind, who.pid or 0, who.app or "-")
end

-- The last @max reports (a list of { serial, text }, oldest first) as the state file's keys
function M.report_add (list, serial, text, max)
  table.insert (list, { serial = serial, text = text })
  while #list > max do
    table.remove (list, 1)
  end
  local props = {}
  for _, r in ipairs (list) do
    props[r.serial] = r.text
  end
  return props
end

-- The account of this WirePlumber from its runtime directory, nil if that is none of logind's
function M.uid_of (runtime_dir)
  return type (runtime_dir) == "string" and runtime_dir:match ("^/run/user/(%d+)/?$") or nil
end

return M
