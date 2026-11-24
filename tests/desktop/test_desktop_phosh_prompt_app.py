# phosh-plinius 0107: a prompt (a PIN for polkit) shows on the screen of the app that asks. polkit
# names the process, its unit in /proc/<pid>/cgroup names the app. app_id_of_pid() and the
# unescaping run here as they are in the patch, compiled against GLib with /proc moved to tmp_path.

import re
import subprocess

import pytest

from conftest import PKG, compile_c

PHOSH = PKG / "phosh-plinius"
PATCH = next(PHOSH.glob("0107-shell-*.patch"))

HARNESS = r"""
#include <glib.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

@CODE@

int
main (int argc, char **argv)
{
  for (int i = 1; i < argc; i++) {
    g_autofree char *app_id = app_id_of_pid ((pid_t) atoi (argv[i]));

    printf ("%s\n", app_id ? app_id : "(none)");
  }
  return 0;
}
"""


def patch_code():
    """The lines 0107 adds to polkit-auth-agent.c, from the unescaping up to prompt_monitor()."""
    text = PATCH.read_text()
    part = text.split("+++ b/src/polkit-auth-agent.c", 1)[1].split("\ndiff --git", 1)[0]
    added = "\n".join(line[1:] for line in part.splitlines() if line.startswith("+"))
    start = added.index("/* systemd's escaping in unit names")
    end = added.index("/*\n * Where the action was taken")
    code = added[start:end]
    # /proc of the test instead of the real one
    assert code.count('"/proc/%d/cgroup", (int) pid') == 1
    return code.replace('"/proc/%d/cgroup", (int) pid',
                        '"%s/%d/cgroup", g_getenv ("FAKE_PROC"), (int) pid')


@pytest.fixture(scope="module")
def app_id_of(tmp_path_factory):
    build = tmp_path_factory.mktemp("prompt-app")
    source = build / "harness.c"
    source.write_text(HARNESS.replace("@CODE@", patch_code()))
    program = compile_c(build / "harness", [source], pkgs=["glib-2.0"])
    proc = tmp_path_factory.mktemp("proc")
    count = iter(range(1, 1000))

    def run(cgroup):
        pid = next(count)
        if cgroup is not None:
            (proc / str(pid)).mkdir()
            (proc / str(pid) / "cgroup").write_text(cgroup)
        r = subprocess.run([str(program), str(pid)], capture_output=True, text=True, check=True,
                           env={"FAKE_PROC": str(proc)})
        out = r.stdout.strip()
        return None if out == "(none)" else out
    return run


APP = "0::/user.slice/user-32011.slice/user@32011.service/app.slice/"


@pytest.mark.parametrize("unit, app_id", [
    # GNOME Settings as it ran on the phone: activated through D-Bus
    (r"app-dbus\x2d:1.2\x2dorg.gnome.Settings.slice/dbus-:1.2-org.gnome.Settings@1.service",
     "org.gnome.Settings"),
    (r"dbus-:1.15-org.gnome.Calls.service", "org.gnome.Calls"),
    # Started by GLib or Phosh, by the desktop's menu, as a Flatpak
    ("app-gnome-org.gnome.Console-4711.scope", "org.gnome.Console"),
    ("app-desktop-firefox-84543.scope", "firefox"),
    (r"app-flatpak-org.foo\x2dbar.App-99.scope", "org.foo-bar.App"),
    (r"app-gnome-plinius\x2dassist-4998.scope", "plinius-assist"),
    # An autostarted app as a service with an instance
    ("app-gnome-org.gnome.Evolution\\x2dalarm\\x2dnotify@autostart.service",
     "org.gnome.Evolution-alarm-notify"),
])
def test_the_unit_names_the_app(app_id_of, unit, app_id):
    assert app_id_of(APP + unit + "\n") == app_id


@pytest.mark.parametrize("cgroup", [
    # The session itself, Phosh's own processes, a system service: no app
    "0::/user.slice/user-32011.slice/session-1.scope\n",
    APP + "plinius-desktop.service\n",
    "0::/system.slice/polkit.service\n",
    # Names that end before an app id
    APP + "app-x.scope\n",
    APP + "dbus-:1.2.service\n",
    APP + "app-gnome-.scope\n",
    # Nothing to read: no slash, an empty file, a process that is gone
    "garbage\n",
    "",
    None,
])
def test_no_app_without_an_app_unit(app_id_of, cgroup):
    assert app_id_of(cgroup) is None


def test_only_the_first_line_counts(app_id_of):
    # cgroup v2 has one line; anything after it is not the unit
    assert app_id_of(APP + "app-gnome-org.gnome.Maps-7.scope\n1:name=x:/app-gnome-other-1.scope\n") \
        == "org.gnome.Maps"


def test_a_broken_escape_stays_as_it_is(app_id_of):
    assert app_id_of(APP + r"app-gnome-a\x2zb-1.scope" + "\n") == r"a\x2zb"
    assert app_id_of(APP + r"app-gnome-ab\x2-1.scope" + "\n") == r"ab\x2"


def test_phosh_asks_on_the_primary_monitor_for_itself():
    # prompt_monitor() leaves the monitor to the system modal (primary) for Phosh's own pid
    text = PATCH.read_text()
    assert re.search(r"\+\s+if \(pid == getpid \(\)\)\n\+\s+return NULL;", text)
