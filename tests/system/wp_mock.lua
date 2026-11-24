-- Minimal stand-ins for the parts of WirePlumber's Lua API that the phone's scripts use, so
-- they run under the host's lua (test_system_wireplumber_lua.py). Only what those scripts call;
-- the semantics follow WirePlumber 0.5 where the scripts depend on them.

HOOKS = {}
LOG = {}

local function logger (topic)
  local l = {}
  local function add (level)
    return function (_, a, b)
      -- log:info (msg) or log:info (object, msg)
      table.insert (LOG, { topic = topic, level = level, msg = b or a })
    end
  end
  l.info, l.warning, l.debug, l.notice = add ("info"), add ("warning"), add ("debug"), add ("notice")
  return l
end
Log = { open_topic = logger }

function Constraint (t) return t end
function EventInterest (t) return t end
function Interest (t) return t end

function SimpleEventHook (def)
  return { register = function () HOOKS [def.name] = def end }
end

-- Only the JSON the scripts write and read: an object of string values
Json = {
  Raw = function (s)
    return { parse = function ()
      local t = {}
      for k, v in tostring (s):gmatch ('"([^"]*)"%s*:%s*"([^"]*)"') do t [k] = v end
      return next (t) and t or nil
    end }
  end,
  Object = function (t)
    return { to_string = function ()
      local keys = {}
      for k in pairs (t) do table.insert (keys, k) end
      table.sort (keys)
      local parts = {}
      for _, k in ipairs (keys) do table.insert (parts, string.format ('"%s":"%s"', k, t [k])) end
      return "{" .. table.concat (parts, ",") .. "}"
    end }
  end,
}

Perm = { ALL = "rwxm", RX = "rx", NONE = "-" }
Features = { ALL = "all" }

PERMISSION_MANAGERS = {}
function PermissionManager ()
  local pm = { matches = {}, active = false }
  function pm:set_default_permissions (p) self.default = p end
  function pm:add_interest_match (fn, interest) table.insert (self.matches, { fn = fn, interest = interest }) end
  function pm:activate (_, cb) self.active = true; if cb then cb (self, nil) end end
  table.insert (PERMISSION_MANAGERS, pm)
  return pm
end

-- Objects: a table with .properties (and "bound-id" for devices)
local function matches (obj, interest)
  for _, c in ipairs (interest) do
    if type (c) == "table" then
      local key, verb, value = c [1], c [2], c [3]
      local have
      if c.type == "gobject" then have = obj [key] else have = obj.properties [key] end
      if verb == "=" and have ~= value then return false end
    end
  end
  return true
end

function ObjectManager (objects)
  local om = { objects = objects or {} }
  function om:lookup (interest)
    for _, o in ipairs (self.objects) do
      if matches (o, interest) then return o end
    end
    return nil
  end
  return om
end

function Metadata (values)
  local m = { values = values or {}, sets = {} }
  m.properties = { ["metadata.name"] = "default" }
  function m:find (subject, key) return self.values [key] end
  function m:set (subject, key, typ, value)
    self.values [key] = value
    table.insert (self.sets, { key = key, type = typ, value = value })
  end
  return m
end

function Node (props)
  local n = { properties = props }
  return n
end

-- A session item linked to a node
function SessionItem (node, props)
  local si = { node = node, properties = props or (node and node.properties) or {} }
  function si:get_associated_proxy (kind) if kind == "node" then return self.node end end
  return si
end

function Client (props)
  local c = { properties = props }
  function c:get_property (k) return self.properties [k] end
  return c
end

function Event (source, subject, data)
  local e = { source = source, subject = subject, data = data or {} }
  function e:get_source () return self.source end
  function e:get_subject () return self.subject end
  function e:get_data (k) return self.data [k] end
  function e:set_data (k, v) self.data [k] = v end
  return e
end

-- The source of events: get-object-manager by name
function Source (oms)
  local s = { oms = oms }
  function s:call (method, name)
    assert (method == "get-object-manager", method)
    return self.oms [name]
  end
  return s
end

-- linking-utils: the select-target event carries source, om, si, si_props, si_flags, target
CAN_LINK = true
package.preload ["linking-utils"] = function ()
  local lutils = {}
  function lutils:unwrap_select_target_event (event)
    local d = event.data
    return event.source, d.om, event.subject, event.subject.properties, d.si_flags, d.target
  end
  function lutils.canLink (props, target) return CAN_LINK end
  return lutils
end

function run_hook (name, event)
  local hook = assert (HOOKS [name], "no hook " .. name)
  hook.execute (event)
  return event
end
