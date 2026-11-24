#!/usr/bin/python3
"""Connection details at the bar's Wi-Fi icon (a click opens, a click next to it closes)."""
import subprocess
import sys

import desk_common as common  # noqa: E402

_ = common._



def nmcli(*args):
    out = subprocess.run(["nmcli", "-t", *args], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line]


def split(line):
    """nmcli -t escapes ':' inside a field as '\\:'."""
    parts, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def details():
    """[(label, value)] of the Wi-Fi connection, or None when there is none."""
    active = [split(l) for l in nmcli("-f", "IN-USE,SSID,SIGNAL,FREQ,SECURITY,RATE",
                                      "dev", "wifi", "list", "--rescan", "no") if l.startswith("*")]
    if not active:
        return None
    # Not "_" for the IN-USE field: that would make gettext's _ local to the whole function
    _in_use, ssid, signal_, freq, security, rate = (active[0] + [""] * 6)[:6]
    band = ""
    try:
        mhz = int(freq.split()[0])
        band = "2.4 GHz" if mhz < 3000 else "5 GHz" if mhz < 5925 else "6 GHz"
    except (ValueError, IndexError):
        pass
    rows = [(_("Network"), ssid), (_("Signal"), f"{signal_} %"), (_("Band"), f"{band} ({freq})" if band else freq),
            (_("Security"), security or _("Open")), (_("Rate"), rate)]
    device = next((split(l)[0] for l in nmcli("-f", "DEVICE,TYPE,STATE", "dev")
                   if split(l)[1:3] == ["wifi", "connected"]), None)
    if device:
        ip = {}
        for l in nmcli("-f", "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS", "dev", "show", device):
            key, _sep, value = l.partition(":")
            ip.setdefault(key.split("[")[0], []).append(value)
        rows += [(_("IP address"), ", ".join(ip.get("IP4.ADDRESS", []))),
                 (_("Gateway"), ", ".join(ip.get("IP4.GATEWAY", []))),
                 (_("DNS"), ", ".join(ip.get("IP4.DNS", [])))]
    return [(k, v) for k, v in rows if v]


def main():
    common.info_popup("plinius-desk-network.pid", _("Wi-Fi"), lambda: details() or _("Not connected"),
                      _("Wi-Fi Settings"), ["gnome-control-center", "wifi"])


if __name__ == "__main__":
    sys.exit(main())
