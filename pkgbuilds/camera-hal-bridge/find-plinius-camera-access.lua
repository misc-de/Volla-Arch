-- Volla Phone Plinius (camera-hal-bridge): the cameras only through the camera portal.
-- SPDX-License-Identifier: MIT
--
-- camera-hal-bridge offers the phone's cameras as PipeWire nodes (hal_camera_*). WirePlumber's
-- default access gave every native client of the session all permissions on them: any program
-- could watch through the camera, without the question GNOME Settings > Privacy > Camera stands
-- for. Now a native client ("unrestricted") gets no permissions on camera nodes, unless it is one
-- of the phone's own parts below. Apps get the camera through the camera portal (xdg-desktop-
-- portal, access "portal": find-portal-access.lua, unchanged), as Snapshot, Firefox and
-- Flatpak apps (Emilia) do; flatpak and restricted clients keep their rules.
--
-- The names come from the client (application.process.binary), so a hostile native program
-- could claim one of them: against native apps this is a brake, against Flatpak apps the portal
-- is the boundary (docs/PLAN-FREIGABE.md, "Grenze").

log = Log.open_topic ("s-client")

-- The phone's own parts that work with the camera nodes themselves
FULL = {
  ["camera-hal-bridge"] = true,   -- makes the nodes
  ["plinius-usb-webcam"] = true,  -- the phone as a USB webcam (switched on by the user)
  ["plinius-usb"] = true,
  ["xdg-desktop-portal"] = true,  -- the camera portal: is there a camera, opens the remote
  ["wireplumber"] = true,
  ["pipewire"] = true,
}
-- Only to see that a camera is there and in use
READ = {
  ["phosh"] = true,
}

camera_pm = PermissionManager ()
camera_pm:set_default_permissions (Perm.ALL)
camera_pm:add_interest_match (
  function (_, client, _)
    local binary = client:get_property ("application.process.binary") or ""
    if FULL[binary] then
      return Perm.ALL
    end
    if READ[binary] then
      return Perm.RX
    end
    log:info (client, string.format ("no camera for native client '%s'", binary))
    return Perm.NONE
  end,
  Interest {
    type = "node",
    Constraint { "media.role", "=", "Camera" },
    Constraint { "media.class", "=", "Video/Source" },
  }
)

SimpleEventHook {
  name = "client/find-plinius-camera-access",
  before = "client/find-default-access",
  after = { "client/find-config-access", "client/find-portal-access" },
  interests = {
    EventInterest {
      Constraint { "event.type", "=", "select-access" },
    },
  },
  execute = function (event)
    local client = event:get_subject ()
    -- Portal, Flatpak or a config rule decided already
    if event:get_data ("permission-manager") ~= nil then
      return
    end
    local access = client:get_property ("pipewire.access")
    if access == nil or access == "unrestricted" or access == "default" then
      event:set_data ("permission-manager", camera_pm)
    end
  end
}:register()

camera_pm:activate (Features.ALL, function (_, e)
  if e then
    log:warning ("failed to activate the camera permission manager: " .. tostring (e))
  end
end)
