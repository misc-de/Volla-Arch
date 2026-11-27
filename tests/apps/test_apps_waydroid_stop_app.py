# waydroid-plinius-stop-app: GNOME Usage quits an Android app through it (pkexec). waydroid is a
# stub whose status comes from FAKE_STATE and which logs its calls; the polkit action, the
# PKGBUILD and the Usage patch must name the same helper.

import xml.etree.ElementTree as ET

import pytest

from conftest import PKG, load_script

DIR = PKG / "waydroid-plinius"
SCRIPT = DIR / "waydroid-plinius-stop-app"
HELPER = "/usr/lib/waydroid-plinius/waydroid-plinius-stop-app"

WAYDROID = r'''
if [ "$1" = status ]; then
	[ -n "$FAKE_STATUS_FAILS" ] && exit 1
	[ -n "$FAKE_STATE" ] && printf 'Session:\tRUNNING\nContainer:\t%s\nVendor type:\tHALIUM_14\n' "$FAKE_STATE"
	exit 0
fi
[ "$1" = container ] && [ -n "$FAKE_UNFREEZE_FAILS" ] && exit 1
[ "$1" = shell ] && [ -n "$FAKE_SHELL_FAILS" ] && exit 1
exit 0
'''


@pytest.fixture
def tool(fake_bin, monkeypatch):
    mod = load_script(SCRIPT)
    fake_bin.stub("waydroid", WAYDROID)
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    monkeypatch.setenv("FAKE_STATE", "RUNNING")
    mod.calls = lambda: [c for c in fake_bin.calls("waydroid") if c != ["status"]]
    return mod


def test_stops_the_app_in_a_running_container(tool):
    assert tool.main(["x", "com.android.gpstest.osmdroid"]) == 0
    assert tool.calls() == [["shell", "--", "am", "force-stop", "com.android.gpstest.osmdroid"]]


def test_a_frozen_container_is_thawed_first(tool, monkeypatch):
    monkeypatch.setenv("FAKE_STATE", "FROZEN")
    assert tool.main(["x", "org.example.app"]) == 0
    assert tool.calls() == [["container", "unfreeze"], ["shell", "--", "am", "force-stop", "org.example.app"]]


def test_nothing_to_stop_without_android(tool, monkeypatch):
    monkeypatch.setenv("FAKE_STATE", "STOPPED")
    assert tool.main(["x", "org.example.app"]) == 0
    assert tool.calls() == []


def test_waydroid_not_answering_fails(tool, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_STATE", "")  # no Container line
    assert tool.main(["x", "org.example.app"]) == 1
    assert "does not answer" in capsys.readouterr().err
    assert tool.calls() == []


def test_waydroid_missing_fails(tool, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert tool.main(["x", "org.example.app"]) == 1


def test_a_thaw_that_fails_stops_nothing(tool, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_STATE", "FROZEN")
    monkeypatch.setenv("FAKE_UNFREEZE_FAILS", "1")
    assert tool.main(["x", "org.example.app"]) == 1
    assert "thaw" in capsys.readouterr().err
    assert tool.calls() == [["container", "unfreeze"]]


def test_a_failed_force_stop_is_reported(tool, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_SHELL_FAILS", "1")
    assert tool.main(["x", "org.example.app"]) == 1
    assert "org.example.app was not stopped" in capsys.readouterr().err


def test_a_hanging_waydroid_counts_as_failed(tool, fake_bin, monkeypatch):
    fake_bin.stub("waydroid", "exec sleep 5")
    monkeypatch.setattr(tool, "TIMEOUT", 0.2)
    assert tool.container_state() is None
    assert tool.run(["waydroid", "shell"]) is False
    assert tool.main(["x", "org.example.app"]) == 1


@pytest.mark.parametrize("argv", [
    [],
    ["org.example.app", "more"],
    ["chrome"],                      # one part only
    ["-a"],                          # an option of am
    ["--user", "0"],
    ["com.example.app;reboot"],
    ["com.example..app"],
    ["com.1example.app"],            # a part must start with a letter
    ["com.example.app/.Main"],       # a component, not a package
    ["c." + "a" * 254],              # longer than 255
])
def test_anything_but_one_package_name_is_refused(tool, argv, capsys):
    assert tool.main(["x"] + argv) == 2
    assert "usage:" in capsys.readouterr().err
    assert tool.calls() == []


def test_package_names_android_allows_are_taken(tool):
    for name in ("com.android.chrome", "org.lineageos.jelly", "de.example_app.App2", "a.b"):
        assert tool.main(["x", name]) == 0


def test_policy_lets_the_active_user_run_only_this_helper():
    action = ET.parse(DIR / "org.plinius.waydroid.policy").getroot().find("action")
    assert action.get("id") == "org.plinius.waydroid.stop-app"
    d = action.find("defaults")
    assert (d.findtext("allow_any"), d.findtext("allow_inactive"), d.findtext("allow_active")) == ("no", "no", "yes")
    assert action.find("annotate[@key='org.freedesktop.policykit.exec.path']").text == HELPER


def test_package_installs_helper_and_policy():
    pkgbuild = (DIR / "PKGBUILD").read_text()
    assert "waydroid-plinius-stop-app \\\n\t\t-t \"$pkgdir/usr/lib/waydroid-plinius\"" in pkgbuild
    assert 'install -Dm644 org.plinius.waydroid.policy -t "$pkgdir/usr/share/polkit-1/actions"' in pkgbuild


def test_usage_calls_this_helper_through_pkexec():
    patch = (PKG / "gnome-usage-plinius" / "0004-app-item-Let-Android-apps-be-quit.patch").read_text()
    assert f'ANDROID_STOP_HELPER = "{HELPER}"' in patch
    assert '"pkexec", ANDROID_STOP_HELPER, (!) package' in patch
    # Android itself: the session ends, as the launcher's own "Stop session" action does
    assert '"waydroid", "session", "stop"' in patch


def test_runs_as_a_program(fake_bin):
    import subprocess
    fake_bin.stub("waydroid", WAYDROID)
    env = fake_bin.env(isolated=True, FAKE_STATE="RUNNING")
    assert subprocess.run([str(SCRIPT), "org.example.app"], env=env).returncode == 0
    assert subprocess.run([str(SCRIPT), "-a"], env=env, capture_output=True).returncode == 2
