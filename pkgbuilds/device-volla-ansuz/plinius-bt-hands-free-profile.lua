-- SPDX-License-Identifier: MIT
-- Plinius: a Bluetooth headset's hands-free profile (HFP/HSP) only for as long as
-- audio-hal-bridge needs it. The bridge switches a headset to hands-free for a call or a
-- recording and back afterwards, without WirePlumber remembering it (save = false). Two things
-- in WirePlumber's profile selection work against that:
--
-- 1. A hands-free profile set by hand (pactl set-card-profile, wpctl set-profile) is stored and
--    comes back at every later connection: the headset then plays at 8 or 16 kHz mono, and
--    media went to the phone's speaker instead (2026-11-18). A stored hands-free profile is
--    ignored here, the best profile wins: A2DP, or hands-free when the device offers nothing
--    else (a car kit).
--
-- 2. WirePlumber selects a profile again whenever the headset's profile list changes, e.g. when
--    a headset that connected with hands-free first brings up its A2DP link while a call already
--    runs on it. The stored or best profile (A2DP) would then take the voice link away in the
--    middle of the call. While the bridge holds a link (its stream audio-hal-bridge.sco-hold
--    exists), a headset in hands-free stays in hands-free; the bridge switches it back itself.
--
-- A headset may also start in hands-free on its own (PipeWire's initial profile when HFP
-- connects before A2DP), so an active hands-free profile alone says nothing about a call.

cutils = require ("common-utils")
log = Log.open_topic ("s-device")

local function hands_free (name)
  return name ~= nil and name:find ("^headset%-head%-unit") ~= nil
end

-- The bridge's hold stream: it exists while a call or a recording uses a headset
local holds_om = ObjectManager {
  Interest {
    type = "node",
    Constraint { "node.name", "=", "audio-hal-bridge.sco-hold" },
  }
}

-- The device's active profile, as its EnumProfile entry (what select-profile hooks pass on)
local function active_profile (device)
  for p in device:iterate_params ("Profile") do
    local active = cutils.parseParam (p, "Profile")
    if active then
      for e in device:iterate_params ("EnumProfile") do
        local profile = cutils.parseParam (e, "EnumProfile")
        if profile and profile.index == active.index then
          return profile
        end
      end
    end
  end
  return nil
end

SimpleEventHook {
  name = "plinius/bt-hands-free-profile",
  after = "device/find-stored-profile",
  before = "device/find-preferred-profile",
  interests = {
    EventInterest {
      Constraint { "event.type", "=", "select-profile" },
    },
  },
  execute = function (event)
    local device = event:get_subject ()
    if device.properties ["device.api"] ~= "bluez5" then
      return
    end
    local dev_name = device.properties ["device.name"] or ""

    if holds_om:lookup () then
      local active = active_profile (device)
      if active and hands_free (active.name) and active.available ~= "no" then
        log:info (device, string.format (
            "keeping hands-free profile '%s' on %s: audio-hal-bridge holds the link",
            active.name, dev_name))
        event:set_data ("selected-profile", active)
        return
      end
    end

    local selected = event:get_data ("selected-profile")
    if selected and hands_free (selected.name) then
      log:info (device, string.format (
          "not restoring hands-free profile '%s' on %s", selected.name, dev_name))
      event:set_data ("selected-profile", nil)
    end
  end
}:register ()

holds_om:activate ()
