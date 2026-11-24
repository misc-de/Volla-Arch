# The phone's WirePlumber configuration: settings PipeWire reads per device must sit in a
# per-device rule, or PipeWire never sees them.

import re

from conftest import PKG

AUDIO_CONF = PKG / "device-volla-ansuz" / "50-plinius-audio.conf"

# The roles of a remote device: what a headset or car kit offers to the phone
REMOTE_ROLES = {"a2dp_sink", "hfp_hf", "hsp_hs"}


def sections(text):
    """Top-level sections of a SPA-JSON .conf file, by name."""
    text = re.sub(r"#[^\n]*", "", text)
    out = {}
    for m in re.finditer(r"^([A-Za-z0-9_.-]+)\s*=\s*([\[{])", text, re.M):
        open_ch = m.group(2)
        close_ch = "]" if open_ch == "[" else "}"
        depth, i = 0, m.end() - 1
        while True:
            c = text[i]
            depth += c == open_ch
            depth -= c == close_ch
            i += 1
            if depth == 0:
                break
        out[m.group(1)] = text[m.end() - 1:i]
    return out


def test_auto_connect_is_set_per_device():
    s = sections(AUDIO_CONF.read_text())
    # bluez5-device.c reads it from the device's properties; the monitor never passes it on
    assert "bluez5.auto-connect" not in s["monitor.bluez.properties"]
    rules = s["monitor.bluez.rules"]
    assert '"~bluez_card.*"' in rules
    roles = re.search(r"bluez5\.auto-connect\s*=\s*\[([^\]]*)\]", rules).group(1).split()
    assert set(roles) == REMOTE_ROLES


def test_roles_stay_the_phone_side():
    s = sections(AUDIO_CONF.read_text())
    roles = re.search(r"bluez5\.roles\s*=\s*\[([^\]]*)\]",
                      s["monitor.bluez.properties"]).group(1).split()
    assert set(roles) == {"a2dp_source", "hfp_ag", "hsp_ag"}
