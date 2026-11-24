-- SPDX-License-Identifier: MIT
-- audio-hal-bridge: while a Bluetooth headset is in hands-free, its voice goes through the vendor
-- HAL, not through BlueZ - on this phone SCO audio never crosses HCI. PipeWire's hands-free sink
-- plays into nothing and its microphone records silence. Streams that would go there go to the
-- bridge instead, which has its HAL streams on Bluetooth SCO by then (see audio-hal-bridge.c).
-- Only the bridge's own hold stream stays on the headset: it keeps the SCO link up.

lutils = require ("linking-utils")
log = Log.open_topic ("s-linking")

SimpleEventHook {
  name = "linking/audio-hal-bridge-sco",
  after = { "linking/find-defined-target",
            "linking/find-filter-target",
            "linking/find-media-role-target",
            "linking/find-media-role-sink-target",
            "linking/find-default-target",
            "linking/find-best-target" },
  before = { "linking/get-filter-from-target",
             "linking/prepare-link" },
  interests = {
    EventInterest {
      Constraint { "event.type", "=", "select-target" },
    },
  },
  execute = function (event)
    local source, om, si, si_props, si_flags, target =
        lutils:unwrap_select_target_event (event)

    -- Not the hold, and not the inner streams of filters and loopbacks: WirePlumber's own
    -- loopback for the headset's microphone stays where it is. The output streams of the role
    -- sinks (55-plinius-media-roles.conf, media.role "Loopback") carry all playback, though:
    -- they go to the HAL like any stream
    if not target or si_props ["node.name"] == "audio-hal-bridge.sco-hold" or
        (si_props ["node.link-group"] and si_props ["media.role"] ~= "Loopback") then
      return
    end
    local node = target:get_associated_proxy ("node")
    local profile = node and node.properties ["api.bluez5.profile"]
    if not profile or not profile:find ("^headset%-head%-unit") then
      return
    end

    -- Playback to the speaker node (routed to the headset then), recording from the bridge's
    -- source for the headset, or the microphone if there is none
    local names = node.properties ["media.class"] == "Audio/Sink" and { "hal_output" }
        or { "hal_headset_input", "hal_input" }
    local hal = nil
    for _, name in ipairs (names) do
      hal = hal or om:lookup {
        type = "SiLinkable",
        Constraint { "node.name", "=", name },
      }
    end
    if hal and lutils.canLink (si_props, hal) then
      log:info (si, string.format ("... %s to the HAL instead of the hands-free %s",
          tostring (si_props ["node.name"]), tostring (node.properties ["node.name"])))
      event:set_data ("target", hal)
    end
  end
}:register ()
