-- The part of WirePlumber's Lua API (0.5) that find-plinius-microphone-access.lua uses, as fakes,
-- so that the hook runs in a plain Lua (tests/daemons-security/test_dsec_appaccess_microphone.py).
-- SPDX-License-Identifier: MIT
--
-- The world the hook sees is in W: W.clients, W.nodes, W.links (objects { ["bound-id"],
-- properties }), W.files (path -> the section's table, "unreadable", or nil for none),
-- W.cgroups (pid -> the first line of its cgroup file), W.env, W.plugins. What the hook does is
-- recorded: W.log, W.saved (state name -> what it saved), W.destroyed (bound-ids), W.calls
-- (plugin calls), W.idle (functions waiting for the idle loop).

W = {
  clients = {}, nodes = {}, links = {}, files = {}, cgroups = {}, env = {}, plugins = {},
  log = {}, saved = {}, destroyed = {}, calls = {}, idle = {}, hooks = {}, oms = {},
}

local function logger (level)
  return function (_, a, b)
    -- log:info (object, message) or log:info (message)
    table.insert (W.log, level .. " " .. tostring (b ~= nil and b or a))
  end
end

Log = {
  open_topic = function (_)
    return { info = logger ("I"), warning = logger ("W"), debug = logger ("D") }
  end,
}

os.getenv = function (name) return W.env[name] end

Perm = { NONE = 0, RX = 500, ALL = 510 }

function Constraint (t) return t end
function Interest (t) return t end
function EventInterest (t) return t end

function SimpleEventHook (t)
  return { register = function () W.hooks[t.name] = t end }
end

function State (name)
  return {
    save = function (_, props)
      local copy = {}
      for k, v in pairs (props) do copy[k] = v end
      W.saved[name] = copy
      return true, ""
    end,
  }
end

function Conf (path, props)
  assert (props["as-section"] == "plinius.microphone" and props["no-fragments"] == "true")
  return {
    open = function ()
      local f = W.files[path]
      if f == nil then
        return "Could not locate configuration file '" .. path .. "'"
      elseif f == "unreadable" then
        return "Permission denied"
      end
      return nil
    end,
    get_section_as_object = function (_, name)
      assert (name == "plinius.microphone")
      return W.files[path]
    end,
  }
end

ProcUtils = {
  get_proc_info = function (pid)
    return { get_cgroup = function () return W.cgroups[pid] end }
  end,
}

Plugin = { find = function (name) return W.plugins[name] end }

Core = { idle_add = function (fn) table.insert (W.idle, fn) end }

-- A glob of WirePlumber's "#" verb: only "*" is used
local function glob (pattern, s)
  local lua = "^" .. pattern:gsub ("[%^%$%(%)%%%.%[%]%+%-%?]", "%%%0"):gsub ("%*", ".*") .. "$"
  return s ~= nil and s:match (lua) ~= nil
end

local function matches (interest, o)
  for _, c in ipairs (interest or {}) do
    local key, verb, value = c[1], c[2], c[3]
    local have = key == "bound-id" and o["bound-id"] or o.properties[key]
    if verb == "=" and have ~= value then
      return false
    elseif verb == "#" and not glob (value, have) then
      return false
    elseif verb == "!" and (have == nil or have == value) then
      return false
    end
  end
  return true
end

local lists = { client = "clients", node = "nodes", link = "links" }

-- ObjectManager { Interest { type = … } }: one interest here
function ObjectManager (interests)
  local om = { list = lists[interests[1].type], handlers = {} }
  function om:connect (signal, fn) self.handlers[signal] = fn end
  function om:activate () end
  function om:lookup (i)
    for _, o in ipairs (W[self.list]) do
      if matches (i, o) then return o end
    end
    return nil
  end
  function om:iterate (i)
    local found = {}
    for _, o in ipairs (W[self.list]) do
      if matches (i, o) then table.insert (found, o) end
    end
    local n = 0
    return function ()
      n = n + 1
      return found[n]
    end
  end
  W.oms[om.list] = om
  return om
end

-- Objects of the world
local serial = 100

function W.object (list, id, props)
  serial = serial + 1
  props["object.serial"] = props["object.serial"] or tostring (serial)
  local o = { ["bound-id"] = id, properties = props }
  function o:request_destroy () table.insert (W.destroyed, self["bound-id"]) end
  table.insert (W[list], o)
  return o
end

function W.remove (list, o)
  for i, x in ipairs (W[list]) do
    if x == o then table.remove (W[list], i) end
  end
  local om = W.oms[list]
  if om and om.handlers["object-removed"] then
    om.handlers["object-removed"] (om, o)
  end
end

-- A permission manager as the other scripts make them, with the default permissions @default
local pms = 0

function W.pm (default)
  pms = pms + 1
  local pm = setmetatable ({ default = default, matches = {}, handlers = {}, updates = 0, n = pms }, {
    __tostring = function (self) return "<WpPermissionManager:" .. self.n .. ">" end,
  })
  function pm:add_interest_match (fn, interest) table.insert (self.matches, { fn = fn, interest = interest }) end
  function pm:get_default_permissions () return self.default end
  function pm:update_permissions () self.updates = self.updates + 1 end
  function pm:connect (signal, fn) self.handlers[signal] = fn end
  -- What @client gets on @node: the first match's answer, else the default
  function pm:permissions (client, node)
    for _, m in ipairs (self.matches) do
      if matches (m.interest, node) then return m.fn (self, client, node) end
    end
    return self.default
  end
  return pm
end

-- The "select-access" event of a client with @pm chosen by the other scripts
function W.select_access (client, pm)
  W.hooks["client/find-plinius-microphone-access"].execute ({
    get_data = function (_, key)
      assert (key == "permission-manager")
      return pm
    end,
  })
end

function W.node_added (node)
  W.hooks["node/plinius-microphone-report"].execute ({ get_subject = function () return node end })
end

function W.run_idle ()
  while #W.idle > 0 do
    local fn = table.remove (W.idle, 1)
    if fn () then table.insert (W.idle, fn) end
  end
end

-- A recording plugin: calls are kept, signals connected can be emitted
function W.plugin (name)
  local p = { handlers = {} }
  function p:connect (signal, fn) self.handlers[signal] = fn end
  function p:call (...)
    table.insert (W.calls, name .. " " .. table.concat ({ ... }, " "))
    return true
  end
  W.plugins[name] = p
  return p
end

-- JSON of plain values, keys sorted, for the test to compare
function W.json (v)
  local t = type (v)
  if t == "table" then
    local n, keys = #v, {}
    for k in pairs (v) do table.insert (keys, k) end
    if n > 0 and n == #keys then
      local parts = {}
      for i = 1, n do parts[i] = W.json (v[i]) end
      return "[" .. table.concat (parts, ",") .. "]"
    end
    table.sort (keys, function (a, b) return tostring (a) < tostring (b) end)
    local parts = {}
    for _, k in ipairs (keys) do
      table.insert (parts, string.format ("%q", tostring (k)) .. ":" .. W.json (v[k]))
    end
    return "{" .. table.concat (parts, ",") .. "}"
  elseif t == "string" then
    return (string.format ("%q", v):gsub ("\\\n", "\\n"))
  elseif v == nil then
    return "null"
  end
  return tostring (v)
end
