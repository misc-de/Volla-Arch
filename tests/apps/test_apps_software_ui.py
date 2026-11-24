# plinius-software's window with real GTK 4 and libadwaita: runs software_ui_cases.py (the cases,
# next to this file) in a pytest of its own on a private broadway display.
#
# GTK can only be initialized once per process, and the test run's process must not open a window
# on the user's display. So a gtk4-broadwayd of this test serves a display whose sockets lie in
# tmp_path (no TCP port), and the inner pytest gets an environment that reaches nothing outside:
# no X11/Wayland display, no session or system bus, XDG directories and HOME in tmp_path,
# GSettings in memory. The launchers the cases open are files in tmp_path whose Exec logs.

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from conftest import REPO

CASES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "software_ui_cases.py")

SCHEMA = """<schemalist>
  <schema id="mobi.phosh.shell" path="/mobi/phosh/shell/">
    <key name="force-adaptive" type="as"><default>[]</default></key>
  </schema>
</schemalist>
"""

LAUNCHERS = {
    # Opened by "Open": logs that it was started
    "org.plinius.uitest.Open": "Name=Open Test\nExec={launch} open\n",
    # Installed but not in the app grid
    "org.plinius.uitest.Hidden": "Name=Hidden Test\nExec={launch} hidden\nNoDisplay=true\n",
    # Already in Phosh's mobile filter by its own entry
    "org.plinius.uitest.Mobile": "Name=Mobile Test\nExec={launch} mobile\nX-Purism-FormFactor=Workstation;Mobile;\n",
    "org.plinius.uitest.KdeMobile": "Name=KDE Mobile Test\nExec={launch} kde\nX-KDE-FormFactor=desktop;mobile\n",
    "org.plinius.uitest.Desktop": "Name=Desktop Test\nExec={launch} desktop\n",
    # The browser the "Website" row opens
    "org.plinius.uitest.Browser": "Name=Browser Test\nExec={launch} browser %u\nMimeType=x-scheme-handler/https;\n",
}


@pytest.fixture
def cleanup():
    paths = []
    yield paths
    for p in paths:
        shutil.rmtree(p, ignore_errors=True)


def test_software_window_cases_under_broadway(tmp_path, cleanup):
    for tool in ("gtk4-broadwayd", "glib-compile-schemas"):
        if shutil.which(tool) is None:
            pytest.skip(f"no {tool} on the host")
    # The display's sockets: a socket path has at most 107 bytes
    runtime = tmp_path / "run"
    if len(str(runtime / "broadway2.socket")) > 100:
        runtime = Path(tempfile.mkdtemp(prefix="plinius-ui-", dir="/tmp"))
        cleanup.append(runtime)
    else:
        runtime.mkdir(mode=0o700)
    home = tmp_path / "home"
    data = home / "share"
    (data / "applications").mkdir(parents=True)
    config = home / "config"
    config.mkdir()
    log = tmp_path / "launched.log"
    launch = tmp_path / "launch.sh"
    launch.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n')
    launch.chmod(0o755)
    for app_id, body in LAUNCHERS.items():
        (data / "applications" / f"{app_id}.desktop").write_text(
            "[Desktop Entry]\nType=Application\n" + body.format(launch=launch))
    (config / "mimeapps.list").write_text(
        "[Default Applications]\nx-scheme-handler/https=org.plinius.uitest.Browser.desktop\n")
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "plinius-uitest.gschema.xml").write_text(SCHEMA)
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    system_data = tmp_path / "system-share"  # XDG_DATA_DIRS: nothing of the host's
    system_data.mkdir()

    env = dict(os.environ)
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "DBUS_SESSION_BUS_ADDRESS",
                 "XDG_CONFIG_DIRS", "XDG_SESSION_TYPE", "GTK_MODULES", "GTK_THEME", "GDK_SCALE",
                 "GDK_DPI_SCALE", "LANGUAGE", "PYTEST_ADDOPTS"):
        env.pop(name, None)
    env.update({
        "GDK_BACKEND": "broadway", "BROADWAY_DISPLAY": ":1", "XDG_RUNTIME_DIR": str(runtime),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={tmp_path}/no-session-bus",
        "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={tmp_path}/no-system-bus",
        "HOME": str(home), "XDG_DATA_HOME": str(data), "XDG_CONFIG_HOME": str(config),
        "XDG_STATE_HOME": str(home / "state"), "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_DATA_DIRS": str(system_data), "XDG_CURRENT_DESKTOP": "plinius-uitest",
        "GSETTINGS_BACKEND": "memory", "GSETTINGS_SCHEMA_DIR": str(schemas),
        "GIO_USE_VFS": "local", "GTK_A11Y": "none", "NO_AT_BRIDGE": "1", "LC_ALL": "C.UTF-8",
        "PLINIUS_UI_LAUNCHED": str(log), "PLINIUS_TEST_OWN_DISPLAY": "1",
    })
    daemon = subprocess.Popen(["gtk4-broadwayd", "--unixsocket", str(runtime / "http"), ":1"],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        end = time.monotonic() + 10
        while not (runtime / "broadway2.socket").exists():
            assert daemon.poll() is None, daemon.stdout.read().decode()
            assert time.monotonic() < end, "gtk4-broadwayd did not start"
            time.sleep(0.02)
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfEX",
                            "-W", "ignore::DeprecationWarning",
                            CASES], cwd=REPO, env=env, capture_output=True, text=True, timeout=600)
    finally:
        daemon.terminate()
        daemon.wait(10)
    summary = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    assert r.returncode == 0, r.stdout[-20000:] + r.stderr[-5000:]
    assert " passed" in summary and "skipped" not in summary, r.stdout[-5000:]
