-- SPDX-License-Identifier: MIT
-- audio-hal-bridge: alerts never play in the earpiece. Whoever picked the earpiece as output
-- still hears the ringtone, an alarm or a notification: they go to the speaker, as on Android.

lutils = require ("linking-utils")
log = Log.open_topic ("s-linking")

-- The roles of feedbackd's theme (Ringtone, Alarm, Emergency) and the usual alert roles
local alert_roles = {
  alarm = true, emergency = true, ringtone = true, notification = true, event = true,
}

-- With role based volumes (device-volla-ansuz 55-plinius-media-roles.conf) a stream plays into
-- the sink of its role, and that sink's output stream (media.role "Loopback") links to the real
-- output: these are the output streams of the alert roles' sinks
local alert_loopbacks = {
  ["output.loopback.sink.role.alarm"] = true,
  ["output.loopback.sink.role.ringtone"] = true,
  ["output.loopback.sink.role.notification"] = true,
  ["output.loopback.sink.role.alert"] = true,
}

SimpleEventHook {
  name = "linking/audio-hal-bridge-alerts",
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

    if not target or target.properties ["node.name"] ~= "hal_earpiece" then
      return
    end
    local role = si_props ["media.role"]
    if not ((role and alert_roles [role:lower ()]) or
        alert_loopbacks [si_props ["node.name"] or ""] or
        si_props ["application.process.binary"] == "feedbackd") then
      return
    end

    local speaker = om:lookup {
      type = "SiLinkable",
      Constraint { "node.name", "=", "hal_output" },
    }
    if speaker and lutils.canLink (si_props, speaker) then
      log:info (si, string.format ("... alert %s (%s) to the speaker instead of the earpiece",
          tostring (si_props ["node.name"]), tostring (role)))
      event:set_data ("target", speaker)
    end
  end
}:register ()
