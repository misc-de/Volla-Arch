-- SPDX-License-Identifier: MIT
-- Plinius: an audio device that connects (a Bluetooth headset or speaker, a USB headset or sound
-- card) becomes the default output, its microphone the default input (USB, or a Bluetooth
-- headset's through audio-hal-bridge). Whoever pairs
-- a headset wants to hear through it. WirePlumber alone keeps the stored choice (the speaker) as
-- long as that one exists, so a new device would never win. Setting default.configured.* puts
-- the device on top of WirePlumber's history: when it goes away, the previous choice (speaker,
-- microphone) comes back by itself, and when it reconnects it wins again.

log = Log.open_topic ("s-default-nodes")

-- The metadata keys per media class
local config_keys = {
  ["Audio/Sink"] = "default.configured.audio.sink",
  ["Audio/Source"] = "default.configured.audio.source",
}

-- Buses whose devices come and go while the phone runs, and the media classes taken from them;
-- the vendor HAL nodes (speaker, earpiece, microphone) have no device object at all.
-- Not the Bluetooth microphone: on this phone SCO audio never crosses HCI (sco_rx stays 0 with
-- a standing eSCO link, docs/HARDWARE.md), the chip hands it to the audio HAL (BTCVSD). The
-- host-side source bluez_input.* records digital silence, so it must not become the default:
-- audio-hal-bridge offers the headset's microphone as a source of its own (handled below).
local hotplug_buses = {
  bluetooth = { ["Audio/Sink"] = true },
  usb = { ["Audio/Sink"] = true, ["Audio/Source"] = true },
}

-- Per device (bound id) and metadata key: the node name this hook made the default during the
-- current connection. A profile change (A2DP <-> headset profile) replaces the device's nodes:
-- the new node takes over only if the user kept the old one, a later manual choice stays.
local handled = {}

local function current_default (metadata, key)
  local value = metadata:find (0, key)
  if not value then
    return nil
  end
  local parsed = Json.Raw (value):parse ()
  return parsed and parsed.name
end

SimpleEventHook {
  name = "plinius/new-device-default",
  interests = {
    EventInterest {
      Constraint { "event.type", "=", "session-item-added" },
      Constraint { "event.session-item.interface", "=", "linkable" },
      Constraint { "media.class", "c", "Audio/Sink", "Audio/Source" },
    },
  },
  execute = function (event)
    local source = event:get_source ()
    local si = event:get_subject ()
    local node = si:get_associated_proxy ("node")
    if not node then
      return
    end
    local props = node.properties
    local name = props ["node.name"]
    local key = config_keys [props ["media.class"]]
    if not (name and key) then
      return
    end
    local metadata_om = source:call ("get-object-manager", "metadata")
    local metadata = metadata_om:lookup { Constraint { "metadata.name", "=", "default" } }
    if not metadata then
      return
    end

    -- A Bluetooth headset's microphone: audio-hal-bridge offers it as a source of its own,
    -- created when the headset connects (its voice goes through the HAL, not through BlueZ)
    if props ["audio-hal-bridge.headset"] == "true" then
      if current_default (metadata, key) ~= name then
        log:info (string.format ("headset microphone %s: making it the default", name))
        metadata:set (0, key, "Spa:String:JSON", Json.Object { ["name"] = name }:to_string ())
      end
      return
    end

    -- Only devices that were just plugged in or connected
    local device_id = props ["device.id"]
    if not device_id then
      return
    end
    local devices_om = source:call ("get-object-manager", "device")
    local device = devices_om:lookup {
      Constraint { "bound-id", "=", tonumber (device_id), type = "gobject" },
    }
    local classes = device and hotplug_buses [device.properties ["device.bus"] or ""]
    if not classes or not classes [props ["media.class"]] then
      return
    end

    local slot = device_id .. "/" .. key
    local previous = handled [slot]
    local current = current_default (metadata, key)
    if current == name then
      handled [slot] = name
      return
    end
    -- Already switched once for this connection and the user chose something else since
    if previous and current ~= previous then
      return
    end

    log:info (string.format ("new %s %s (%s): making it the default",
        props ["media.class"], name, tostring (device.properties ["device.name"])))
    handled [slot] = name
    metadata:set (0, key, "Spa:String:JSON", Json.Object { ["name"] = name }:to_string ())
  end
}:register ()

-- Forget a device when it disconnects: the next connection switches again
SimpleEventHook {
  name = "plinius/new-device-default-forget",
  interests = {
    EventInterest {
      Constraint { "event.type", "=", "device-removed" },
    },
  },
  execute = function (event)
    local id = tostring (event:get_subject ()["bound-id"])
    for _, key in pairs (config_keys) do
      handled [id .. "/" .. key] = nil
    end
  end
}:register ()
