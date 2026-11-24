#!/usr/bin/python3
"""Battery details at the bar's battery icon (a click opens, a click next to it closes)."""
import locale
import subprocess
import sys

import desk_common as common  # noqa: E402

_ = common._

locale.setlocale(locale.LC_ALL, "")

STATES = {"charging": _("Charging"), "discharging": _("On battery"), "fully-charged": _("Fully charged"),
          "pending-charge": _("Plugged in, not charging"), "pending-discharge": _("Plugged in"),
          "empty": _("Empty")}


def details():
    devices = subprocess.run(["upower", "-e"], capture_output=True, text=True).stdout.split()
    battery = next((d for d in devices if "/battery_" in d), None)
    if battery is None:
        return _("No battery")
    info = {}
    for line in subprocess.run(["upower", "-i", battery], capture_output=True, text=True).stdout.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and value.strip() and key not in info:
            info[key] = value.strip()
    def number(key):
        """upower writes numbers in the locale (here 4,417), maybe with a unit after them."""
        try:
            return float(info.get(key, "").split()[0].rstrip("%").replace(",", "."))
        except (ValueError, IndexError):
            return None

    def fmt(value, digits, unit):
        # The decimal separator of the phone's language
        return None if value is None else f"{locale.format_string(f'%.{digits}f', value)} {unit}"

    rows = [(_("Level"), fmt(number("percentage"), 0, "%")),
            (_("State"), STATES.get(info.get("state", ""), info.get("state", "")))]
    if "time to empty" in info:
        rows.append((_("Time left"), info["time to empty"]))
    if "time to full" in info:
        rows.append((_("Full in"), info["time to full"]))
    rows += [(_("Power"), fmt(number("energy-rate"), 2, "W")),
             (_("Voltage"), fmt(number("voltage"), 2, "V")),
             (_("Temperature"), fmt(number("temperature"), 0, "°C")),
             (_("Health"), fmt(number("capacity"), 0, "%")),
             (_("Charge cycles"), info.get("charge-cycles", "")),
             (_("Technology"), info.get("technology", "").replace("-", " ").title())]
    return [(k, v) for k, v in rows if v]


if __name__ == "__main__":
    sys.exit(common.info_popup("plinius-desk-battery.pid", _("Battery"), details,
                               _("Power Settings"), ["gnome-control-center", "power"]))
