#!/usr/bin/env python3
# Which parts of the repository have tests: own code per package and risk category (lines by
# language, patches), whether the package has a check() and how many tests under tests/ name it.
#
# Usage: tools/test-inventory.py [--markdown]
#
# A test file counts for a package when it names the package's directory (pkgbuilds/NAME or
# PKG / "NAME"); for tools/ when it names the tool file.

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Risk categories: what goes wrong when the code is wrong
CATEGORIES = [
    ("boot", "Boot, encryption, kernel (phone does not start, data lost)", [
        "plinius-crypt", "plinius-crypt-ramdisk", "plinius-kernel", "plinius-kmod",
        "plinius-rollback", "plinius-bootcheck", "hybris-tls-relocate", "plinius-progress",
        "buffybox", "plinius-account", "plinius-home", "plinius-swap", "lxc-android-halium",
        "libhybris"]),
    ("security", "Access and security (lock screen, permissions, network filter)", [
        "plinius-pinguard", "plinius-pattern", "plinius-face", "biomd", "plinius-security",
        "plinius-dnsfilter", "plinius-apparmor", "plinius-ssh", "plinius-usb",
        "plinius-login-keyring", "plinius-netlog", "plinius-widevine", "plinius-udfps",
        "wayvnc-plinius"]),
    ("release", "Updates and releases (what reaches the phone)", [
        "tools", "plinius-software", "plinius-freigabe", "plinius-keyring", "plinius-health"]),
    ("telephony", "Telephony, modem, emergency calls", [
        "ofono2mm", "ofono-binder-plugin", "ofono-binder-plugin-ext-mtk", "ofono-sailfish",
        "mmsd4ofono", "gnome-calls-plinius", "chatty-plinius", "plinius-satellite",
        "plinius-callfocus", "plinius-aml", "lpac", "libgbinder-radio", "furios-gps-contribute"]),
    ("hardware", "Hardware bridges and power", [
        "audio-hal-bridge", "camera-hal-bridge", "gnss-nmea-bridge", "sensor-proxy-bridge",
        "vibrator-ff-bridge", "plinius-energy", "plinius-sar", "plinius-wake", "plinius-charge",
        "plinius-suspend", "plinius-mce", "plinius-torch", "plinius-backlight-wake",
        "hybris-gbm", "hybris-vulkan", "gl4es", "vkmark-plinius", "plinius-tflite", "plinius-vdec", "device-volla-ansuz", "bluebinder",
        "pipewire-audio-plinius", "furios-audio-bt-reconnect", "bluez-obex-plinius",
        "wpa_supplicant-plinius", "nfcd", "libncicore", "plinius-dictate", "cups-plinius",
        "plinius-pdf-printer"]),
    ("ui", "User interface (shell, settings, apps)", [
        "gnome-control-center-plinius", "phosh-plinius", "phosh-tour-plinius", "phoc-plinius",
        "stevia-plinius", "snapshot-plinius", "libhandy-plinius", "gtk3-plinius", "plinius-desktop",
        "plinius-remote", "plinius-cardshare", "plinius-screentime", "plinius-ambient",
        "plinius-assist",
        "plinius-apps", "plinius-nfc-lab", "gnome-usage-plinius", "gnome-online-accounts-plinius",
        "gnome-bluetooth-plinius", "xdg-desktop-portal-wlr-plinius", "mobile-config-firefox",
        "muga", "schwupp"]),
]
CODE = {".c": "C", ".cc": "C", ".cpp": "C", ".h": "C", ".py": "Python", ".sh": "Shell",
        ".lua": "Lua", ".js": "JS", ".install": "Shell", ".hook": "Shell"}


def git_files():
    out = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"], check=True,
                         capture_output=True).stdout.decode()
    return [f for f in out.split("\0") if f]


def language(path):
    ext = os.path.splitext(path)[1]
    if os.path.basename(path).startswith("test-") or path.startswith("tests/"):
        return None
    if ext in CODE:
        if ext == ".hook":
            with open(os.path.join(ROOT, path), errors="replace") as f:
                if "[Trigger]" in f.read():
                    return None
        return CODE[ext]
    if ext:
        return None
    try:
        with open(os.path.join(ROOT, path), "rb") as f:
            first = f.readline(200).decode("utf-8", "replace")
    except OSError:
        return None
    if first.startswith("#!"):
        if "python" in first:
            return "Python"
        if re.search(r"\b(ba)?sh\b", first):
            return "Shell"
    return None


def lines(path):
    with open(os.path.join(ROOT, path), "rb") as f:
        return sum(1 for _ in f)


def count_tests(text):
    return len(re.findall(r"^\s*(?:async\s+)?def test_", text, re.M))


def main():
    files = git_files()
    pkgs = {}
    for f in files:
        parts = f.split("/")
        if parts[0] == "pkgbuilds" and len(parts) > 2:
            name = parts[1]
        elif parts[0] == "tools":
            name = "tools"
        else:
            continue
        p = pkgs.setdefault(name, {"loc": {}, "patches": 0, "check": False, "tests": 0,
                                   "own_tests": 0})
        if f.endswith(".patch"):
            p["patches"] += 1
            continue
        if parts[-1] == "PKGBUILD":
            with open(os.path.join(ROOT, f)) as fh:
                p["check"] = bool(re.search(r"^check\(\)", fh.read(), re.M))
            continue
        if os.path.basename(f).startswith("test-"):
            p["own_tests"] += 1
        lang = language(f)
        if lang:
            p["loc"][lang] = p["loc"].get(lang, 0) + lines(f)

    tests = {}
    for dirpath, _, names in os.walk(os.path.join(ROOT, "tests")):
        for n in names:
            if n.startswith("test_") and n.endswith(".py"):
                with open(os.path.join(dirpath, n)) as fh:
                    tests[os.path.join(dirpath, n)] = fh.read()
    for name, p in pkgs.items():
        if name == "tools":
            pat = re.compile(r"tools/|TOOLS\s*/")
        else:
            pat = re.compile(rf"pkgbuilds/{re.escape(name)}\b|PKG\s*/\s*[\"']{re.escape(name)}[\"']")
        for path, text in tests.items():
            if "/tests/lint/" in path:
                continue
            if pat.search(text):
                p["tests"] += count_tests(text)

    md = "--markdown" in sys.argv
    known = {n for _, _, names in CATEGORIES for n in names}
    cats = CATEGORIES + [("other", "Upstream builds without own code", sorted(set(pkgs) - known))]
    total = {"loc": 0, "tested_loc": 0, "pkgs": 0, "tested": 0}
    for key, title, names in cats:
        rows = [(n, pkgs[n]) for n in names if n in pkgs]
        if not rows:
            continue
        print(f"\n## {title} [{key}]\n" if md else f"\n== {title} [{key}]")
        if md:
            print("| Package | Own code (lines) | Patches | check() | Tests under tests/ |")
            print("|---|---|---|---|---|")
        loc_sum = tested_sum = 0
        for n, p in sorted(rows, key=lambda r: -sum(r[1]["loc"].values())):
            loc = sum(p["loc"].values())
            langs = ", ".join(f"{k} {v}" for k, v in sorted(p["loc"].items(), key=lambda x: -x[1]))
            has = p["tests"] > 0 or p["check"]
            loc_sum += loc
            tested_sum += loc if has else 0
            total["pkgs"] += 1 if loc or p["patches"] else 0
            total["tested"] += 1 if has and (loc or p["patches"]) else 0
            if md:
                print(f"| {n} | {loc} ({langs or '–'}) | {p['patches']} | "
                      f"{'yes' if p['check'] else '–'} | {p['tests'] or '–'} |")
            else:
                print(f"  {n:34} {loc:6} lines  {p['patches']:3} patches  "
                      f"check={'yes' if p['check'] else 'no ':3}  tests={p['tests']:4}  {langs}")
        total["loc"] += loc_sum
        total["tested_loc"] += tested_sum
        share = 100 * tested_sum / loc_sum if loc_sum else 0
        print(f"\n{share:.0f} % of {loc_sum} lines in packages with tests" if md else
              f"  -> {share:.0f} % of {loc_sum} lines in packages with tests")
    share = 100 * total["tested_loc"] / total["loc"] if total["loc"] else 0
    print(f"\nTotal: {total['tested']}/{total['pkgs']} packages with own code or patches have "
          f"tests; {share:.0f} % of {total['loc']} own lines are in packages with tests; "
          f"{sum(count_tests(t) for t in tests.values())} test functions under tests/")


if __name__ == "__main__":
    main()
