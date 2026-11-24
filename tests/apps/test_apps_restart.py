# plinius-restart (pkgbuilds/plinius-software): which restart an update needs (sorting of
# processes with outdated files into apps / ui / reboot) and what "apply" restarts.
#
# /proc is never read for real: the process table is a dict served through the module's own
# read()/read_link(); systemctl and pacman are replaced; apply runs in its dry-run mode.

import json
import os
import subprocess
import sys
import time
import types

import pytest

from conftest import PKG, load_script

SCRIPT = PKG / "plinius-software" / "plinius-restart"
UID = 54321  # no such user on the host: find_desktop does not look into a real home


@pytest.fixture(scope="module")
def mod():
    return load_script(SCRIPT)


# --- Names -----------------------------------------------------------------------------------

def test_unescape(mod):
    assert mod.unescape(r"app-gnome-org.gnome.Calls\x2dDaemon-12.scope") == \
        "app-gnome-org.gnome.Calls-Daemon-12.scope"


@pytest.mark.parametrize("unit, parents, expected", [
    ("app-gnome-org.gnome.Calculator-1234.scope", [], "org.gnome.Calculator"),
    (r"app-gnome-org.gnome.Calls\x2dDaemon-99.scope", [], "org.gnome.Calls-Daemon"),
    ("app-flatpak-org.mozilla.firefox@abc.service", [], "org.mozilla.firefox"),
    ("dbus-:1.2-org.gnome.Nautilus@0.service", [], "org.gnome.Nautilus"),
    ("dbus-:1.2-org.gnome.Nautilus@0.service", ["app.slice", "app-dbus-:1.3-org.gnome.Nautilus.slice"],
     "org.gnome.Nautilus"),
    ("something.service", ["app.slice", "app-dbus-:1.3-org.gnome.Maps.slice"], "org.gnome.Maps"),
    ("mobi.phosh.Shell.service", ["session.slice"], None),
    ("app-gnome-noid.scope", [], None),
])
def test_app_id_of(mod, unit, parents, expected):
    assert mod.app_id_of(unit, parents) == expected


def test_changed_units(mod, monkeypatch):
    monkeypatch.setattr(mod, "UNIT_DIRS", {"system": "/s", "user": "/u"})
    before = {"/s/a.service": "1", "/s/gone.timer": "1", "/s/b.service.d/x.conf": "1",
              "/u/c.socket": "1", "/s/same.service": "1", "/s/tmpl@.service": "1"}
    after = {"/s/a.service": "2", "/s/b.service.d/x.conf": "2", "/u/c.socket": "1",
             "/s/same.service": "1", "/s/new.path": "1", "/s/tmpl@.service": "2",
             "/s/notes.txt": "1", "/s/deep/dir/x.service": "1", "/u/d.service.d/y.conf": "1"}
    assert sorted(unit for _path, unit in mod.changed_units(after, before)) == [
        ("system", "a.service"), ("system", "b.service"), ("system", "gone.timer"),
        ("system", "new.path"), ("user", "d.service")]


def test_unit_digests_skip_links(mod, tmp_path, monkeypatch):
    (tmp_path / "s" / "x.service.d").mkdir(parents=True)
    (tmp_path / "s" / "a.service").write_text("A")
    (tmp_path / "s" / "x.service.d" / "o.conf").write_text("O")
    (tmp_path / "s" / "alias.service").symlink_to("a.service")
    monkeypatch.setattr(mod, "UNIT_DIRS", {"system": str(tmp_path / "s"), "user": str(tmp_path / "missing")})
    digests = mod.unit_digests()
    assert sorted(os.path.relpath(p, tmp_path) for p in digests) == ["s/a.service", "s/x.service.d/o.conf"]
    assert digests[str(tmp_path / "s" / "a.service")] == \
        "559aead08264d5795d3909718cdd05abd49572e84fe55590eef31a88a08fdffd"


# --- Desktop files ----------------------------------------------------------------------------

@pytest.fixture
def desktop_dirs(mod, tmp_path, monkeypatch):
    apps, autostart = tmp_path / "applications", tmp_path / "autostart"
    apps.mkdir()
    autostart.mkdir()
    monkeypatch.setattr(mod, "APP_DIRS", [str(apps)])
    monkeypatch.setattr(mod, "AUTOSTART_DIRS", [str(autostart)])
    return apps, autostart


def test_find_desktop(mod, desktop_dirs):
    apps, autostart = desktop_dirs
    (apps / "org.a.desktop").write_text("[Desktop Entry]\nName=A\n")
    (apps / "org.hidden.desktop").write_text("[Desktop Entry]\nName=H\nHidden=true\n")
    (apps / "org.broken.desktop").write_text("no group at all\n")
    (autostart / "org.helper.desktop").write_text("[Desktop Entry]\nName=Helper\n")
    assert mod.find_desktop("org.a", UID) == str(apps / "org.a.desktop")
    assert mod.find_desktop("org.hidden", UID) is None
    assert mod.find_desktop("org.broken", UID) is None
    assert mod.find_desktop("org.helper", UID) is None
    assert mod.find_desktop("org.helper", UID, autostart=True) == str(autostart / "org.helper.desktop")


# --- Outdated files of one process -----------------------------------------------------------

@pytest.fixture
def proc(mod, monkeypatch, tmp_path):
    """A fake /proc: files and links by path; tmp_path is read for real."""
    files, links = {}, {}

    def read(path, mode="r"):
        if path in files:
            return files[path]
        if str(path).startswith(str(tmp_path)):
            try:
                with open(path, mode) as f:
                    return f.read()
            except OSError:
                return None
        return None
    monkeypatch.setattr(mod, "read", read)
    monkeypatch.setattr(mod, "read_link", lambda path: links.get(path))
    return files, links


def maps(*lines):
    return "".join(f"7f00-7f01 {perm} 00000000 fd:00 1 {path}\n" for perm, path in lines)


def test_stale_reason_deleted_executable(mod, proc):
    files, links = proc
    links["/proc/5/exe"] = "/usr/bin/phosh (deleted)"
    assert mod.stale_reason(5, 0) == "/usr/bin/phosh"
    # Outside /usr (a test build in /tmp): not an update
    links["/proc/5/exe"] = "/tmp/a.out (deleted)"
    assert mod.stale_reason(5, 0) is None
    del links["/proc/5/exe"]
    assert mod.stale_reason(5, 0) is None  # kernel thread or gone


def test_stale_reason_only_executable_mappings_count(mod, proc):
    files, links = proc
    links["/proc/5/exe"] = "/usr/bin/gnome-calls"
    files["/proc/5/maps"] = maps(("r--p", "/usr/share/glib-2.0/schemas/gschemas.compiled (deleted)"),
                                 ("r--p", "/usr/share/icons/hicolor/icon-theme.cache (deleted)"))
    assert mod.stale_reason(5, 0) is None
    files["/proc/5/maps"] += maps(("r-xp", "/home/u/.cache/x.so (deleted)"))
    assert mod.stale_reason(5, 0) is None
    files["/proc/5/maps"] += maps(("r-xp", "/usr/lib/libgtk-4.so.1.2000.0 (deleted)"))
    assert mod.stale_reason(5, 0) == "/usr/lib/libgtk-4.so.1.2000.0"
    files["/proc/5/maps"] = maps(("r-xp", "/app/lib/libfoo.so (deleted)"))
    assert mod.stale_reason(5, 0) == "/app/lib/libfoo.so"


def test_flatpak_stale(mod, proc, tmp_path):
    files, links = proc
    deploy = tmp_path / "app" / "x86_64" / "stable"
    (deploy / "abc").mkdir(parents=True)
    (deploy / "def").mkdir()
    (deploy / "active").symlink_to("abc")
    files["/proc/7/root/.flatpak-info"] = f"[Instance]\napp-path={deploy}/abc/files\n"
    assert mod.flatpak_stale(7) is None
    (deploy / "active").unlink()
    (deploy / "active").symlink_to("def")
    assert mod.flatpak_stale(7) == "app updated"
    files["/proc/7/root/.flatpak-info"] = "not ini ["
    assert mod.flatpak_stale(7) is None


def stat_line(pid, comm, ppid, start=0):
    return f"{pid} ({comm}) S {ppid} " + " ".join(["0"] * 17) + f" {start} 0 0\n"


def test_stale_reason_script_newer_than_its_interpreter(mod, proc):
    files, links = proc
    script = "/usr/bin/env"  # any real file below /usr: only its ctime is looked at
    links["/proc/9/exe"] = "/usr/bin/python3.14"
    files["/proc/9/cmdline"] = f"python3\0-I\0{script}\0--flag\0"
    files["/proc/9/stat"] = stat_line(9, "python3", 1, start=0)
    assert mod.stale_reason(9, 0) == script
    # Started after the script was written
    files["/proc/9/stat"] = stat_line(9, "python3", 1, start=10 ** 12)
    assert mod.stale_reason(9, 0) is None
    # Not an interpreter: its arguments are not scripts
    links["/proc/9/exe"] = "/usr/bin/gnome-shell"
    files["/proc/9/stat"] = stat_line(9, "x", 1, start=0)
    assert mod.stale_reason(9, 0) is None


def test_remote_session(mod, proc):
    files, _links = proc
    files["/run/systemd/sessions/3"] = "TYPE=wayland\nREMOTE=0\n"
    files["/run/systemd/sessions/4"] = "TYPE=tty\nREMOTE=1\n"
    assert not mod.remote_session("session-3.scope")
    assert mod.remote_session("session-4.scope")
    assert not mod.remote_session("user@1000.service")


def test_ancestors_excluded(mod):
    parents = {10: 9, 9: 8, 8: 1, 20: 1}
    assert mod.ancestors_excluded(10, {8}, parents)
    assert not mod.ancestors_excluded(20, {8}, parents)
    assert not mod.ancestors_excluded(1, {1}, parents)


# --- check: the whole classification ---------------------------------------------------------

@pytest.fixture
def system(mod, proc, desktop_dirs, tmp_path, monkeypatch):
    """A process table to classify. add(pid, comm, cgroup, exe=..., ppid=...)."""
    files, links = proc
    apps, autostart = desktop_dirs
    (apps / "org.gnome.Calculator.desktop").write_text("[Desktop Entry]\nName=Calculator\n")
    (autostart / "org.helper.desktop").write_text("[Desktop Entry]\nName=Helper\n")
    (apps / "org.helper.desktop").write_text("[Desktop Entry]\nName=Helper\n")
    pids = []
    files["/proc/stat"] = "cpu 0\nbtime 1000\n"
    files["/proc/self/cgroup"] = "0::/system.slice/plinius-pkg.service\n"
    real_listdir = os.listdir
    monkeypatch.setattr(mod.os, "listdir",
                        lambda path=".": [str(p) for p in pids] if path == "/proc" else real_listdir(path))
    monkeypatch.setattr(mod, "session_uids", lambda: [UID])
    for name in ("KERNEL_TRIAL", "RAMDISK_TRIAL", "RAMDISK_WAITING"):
        monkeypatch.setattr(mod, name, str(tmp_path / name.lower()))

    def add(pid, comm, cgroup, exe="/usr/bin/x (deleted)", ppid=1):
        pids.append(pid)
        files[f"/proc/{pid}/stat"] = stat_line(pid, comm, ppid, start=5)
        files[f"/proc/{pid}/cgroup"] = f"0::{cgroup}\n"
        links[f"/proc/{pid}/exe"] = exe
    return add


USER = f"/user.slice/user-{UID}.slice/user@{UID}.service"


def test_check_nothing_outdated(mod, system):
    system(100, "calc", f"{USER}/app.slice/app-gnome-org.gnome.Calculator-1.scope", exe="/usr/bin/calc")
    assert mod.check() == {"level": "none", "apps": [], "units": [], "reboot": []}


def test_check_apps_only(mod, system, desktop_dirs):
    apps, _ = desktop_dirs
    system(100, "calc", f"{USER}/app.slice/app-gnome-org.gnome.Calculator-1.scope")
    system(101, "calc-helper", f"{USER}/app.slice/app-gnome-org.gnome.Calculator-1.scope")
    result = mod.check()
    assert result["level"] == "apps"
    assert result["apps"] == [{"id": "org.gnome.Calculator", "unit": "app-gnome-org.gnome.Calculator-1.scope",
                               "uid": UID, "desktop": str(apps / "org.gnome.Calculator.desktop"),
                               "reasons": ["calc uses /usr/bin/x", "calc-helper uses /usr/bin/x"]}]


def test_check_session_services_mean_ui(mod, system, desktop_dirs):
    _, autostart = desktop_dirs
    system(100, "calc", f"{USER}/app.slice/app-gnome-org.gnome.Calculator-1.scope")
    system(110, "phosh", f"{USER}/session.slice/mobi.phosh.Shell.service")
    system(111, "helper", f"{USER}/app.slice/app-gnome-org.helper-5.scope")
    result = mod.check()
    assert result["level"] == "ui"
    assert [a["id"] for a in result["apps"]] == ["org.gnome.Calculator"]
    units = {u["unit"]: u for u in result["units"]}
    assert units["mobi.phosh.Shell.service"]["kind"] == "service"
    # An autostarted helper counts as a unit, restarted from its autostart file
    assert units["app-gnome-org.helper-5.scope"]["kind"] == "helper"
    assert units["app-gnome-org.helper-5.scope"]["desktop"] == str(autostart / "org.helper.desktop")


@pytest.mark.parametrize("comm, cgroup, why", [
    ("NetworkManager", "/system.slice/NetworkManager.service", "system: NetworkManager.service"),
    ("phoc", f"/user.slice/user-{UID}.slice/session-2.scope", "session: phoc"),
    ("dbus-broker", f"{USER}/session.slice/dbus-broker.service", "session: dbus-broker.service"),
    ("systemd", f"{USER}/init.scope", "session: init.scope"),
    ("bash", f"{USER}/app.slice/run-u12.scope", "session: run-u12.scope"),
    ("systemd", USER, "session: systemd"),
])
def test_check_reboot_cases(mod, system, comm, cgroup, why):
    system(100, "calc", f"{USER}/app.slice/app-gnome-org.gnome.Calculator-1.scope")
    system(200, comm, cgroup)
    result = mod.check()
    assert result["level"] == "reboot"
    assert any(r.startswith(why) for r in result["reboot"]), result["reboot"]


def test_check_ignores_ssh_sessions_and_its_own_callers(mod, system, proc):
    files, _links = proc
    files["/run/systemd/sessions/7"] = "TYPE=tty\nREMOTE=1\n"
    system(300, "sshd-session", f"/user.slice/user-{UID}.slice/session-7.scope")
    system(301, "pkexec", "/system.slice/plinius-pkg-relay.scope")
    system(302, "bash", "/system.slice/plinius-pkg-relay.scope", ppid=301)
    system(303, "pacman", "/system.slice/plinius-pkg.service")  # same cgroup as this check
    assert mod.check()["level"] == "none"


def test_check_trials_and_boot_files_need_a_reboot(mod, system, tmp_path, monkeypatch):
    with open(tmp_path / "kernel_trial", "w") as f:
        f.write("6.1.193-plinius1\n")
    result = mod.check()
    assert result["level"] == "reboot"
    assert result["reboot"] == ["a new kernel waits for its trial start (6.1.193-plinius1)"]
    os.remove(tmp_path / "kernel_trial")
    (tmp_path / "ramdisk_waiting").write_text("")
    assert mod.check()["reboot"] == ["a new password prompt for the startup waits for its trial start"]
    os.remove(tmp_path / "ramdisk_waiting")

    changed = tmp_path / "changed"
    changed.write_text("linux-firmware\nmesa\n")
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        out = {"mesa": "/usr/lib/libGL.so\n", "linux-firmware": "/usr/lib/firmware/\n/usr/lib/firmware/a.bin\n"}
        return subprocess.CompletedProcess(cmd, 0, "".join(out[n] for n in cmd[2:]), "")
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod.check(str(changed))["reboot"] == ["boot file updated: /usr/lib/firmware/a.bin"]
    assert calls == [["pacman", "-Qlq", "linux-firmware", "mesa"]]
    changed.write_text("mesa\n")
    assert mod.check(str(changed))["level"] == "none"


def test_check_changed_unit_files(mod, system, tmp_path, monkeypatch):
    s, u = tmp_path / "sys", tmp_path / "usr"
    s.mkdir()
    u.mkdir()
    monkeypatch.setattr(mod, "UNIT_DIRS", {"system": str(s), "user": str(u)})
    (s / "bluetooth.service").write_text("old")
    (s / "idle.service").write_text("old")
    (u / "feedbackd.service").write_text("old")
    (u / "dbus-broker.service").write_text("old")
    (u / "stopped.service").write_text("old")
    before = tmp_path / "before.json"
    before.write_text(json.dumps(mod.unit_digests()))
    for f in (s / "bluetooth.service", s / "idle.service", u / "feedbackd.service",
              u / "dbus-broker.service", u / "stopped.service"):
        f.write_text("new")
    active = {"bluetooth.service"}
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0 if cmd[-1] in active else 3))
    monkeypatch.setattr(mod, "user_unit_running",
                        lambda uid, unit: unit in ("feedbackd.service", "dbus-broker.service"))
    result = mod.check(units_before=str(before))
    assert result["reboot"] == ["system: bluetooth.service runs with its old unit file",
                                "session: dbus-broker.service runs with its old unit file"]
    assert [u["unit"] for u in result["units"]] == ["feedbackd.service"]


# --- apply (dry run) -------------------------------------------------------------------------

@pytest.fixture
def apply(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "DRY_RUN", True)
    props = {"mobi.phosh.Shell.service": "RefuseManualStop=yes\nRestart=on-failure\n",
             "stuck.service": "RefuseManualStop=yes\nRestart=no\n"}
    inactive = {"app-gnome-org.gone-1.scope"}

    def systemctl(*args):
        if args[0] == "is-active":
            return subprocess.CompletedProcess(args, 3 if args[1] in inactive else 0, "", "")
        if args[0] == "show":
            return subprocess.CompletedProcess(args, 0, props.get(args[-1], "RefuseManualStop=no\n"), "")
        raise AssertionError(f"systemctl {args} in a dry run")
    monkeypatch.setattr(mod, "systemctl", systemctl)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: pytest.fail("Popen in a dry run"))

    def run(level, result):
        path = tmp_path / "result.json"
        path.write_text(json.dumps(result))
        mod.apply(level, str(path))
        return capsys.readouterr().out.splitlines()
    return run


def unit(name, kind="service", desktop=None, uid=None):
    return {"unit": name, "uid": os.getuid() if uid is None else uid, "kind": kind, "desktop": desktop}


def app_entry(id_, uid=None):
    return {"id": id_, "unit": f"app-gnome-{id_}-1.scope", "uid": os.getuid() if uid is None else uid,
            "desktop": f"/usr/share/applications/{id_}.desktop"}


RESULT = {
    "apps": [app_entry("org.gnome.Settings"), app_entry("org.a"), app_entry("org.gone"),
             app_entry("org.other", uid=os.getuid() + 1)],
    "units": [unit("mobi.phosh.Shell.service"), unit("feedbackd.service"),
              unit("app-gnome-org.helper-5.scope", kind="helper",
                   desktop="/etc/xdg/autostart/org.helper.desktop"),
              unit("stuck.service"), unit("dbus-:1.5-org.x@0.service")],
}


def test_apply_apps_restarts_only_own_running_apps_settings_last(mod, apply):
    out = apply("apps", RESULT)
    pid = os.getpid()
    assert out == [
        "systemctl --user stop --no-block app-gnome-org.a-1.scope",
        f"systemd-run --user --scope --collect --quiet --unit=app-gnome-org.a-{pid}0.scope -- "
        "gio launch /usr/share/applications/org.a.desktop",
        "systemctl --user stop --no-block app-gnome-org.gnome.Settings-1.scope",
        f"systemd-run --user --scope --collect --quiet --unit=app-gnome-org.gnome.Settings-{pid}1.scope -- "
        "gnome-control-center updates",
    ]


def test_apply_ui_restarts_units_then_apps_then_the_shell(mod, apply):
    out = apply("ui", RESULT)
    assert out[0] == "systemctl --user restart feedbackd.service"
    assert out[1] == "systemctl --user stop app-gnome-org.helper-5.scope"
    assert out[2].endswith("gio launch /etc/xdg/autostart/org.helper.desktop")
    assert r"--unit=app-gnome-org.helper" in out[2]
    # RefuseManualStop without Restart=: left alone; D-Bus activated: only stopped
    assert out[3] == "systemctl --user stop dbus-:1.5-org.x@0.service"
    assert "stuck.service" not in "\n".join(out)
    # The shell last, through its Restart=on-failure
    assert out[-1] == "systemctl --user kill --kill-whom=main --signal=SIGKILL mobi.phosh.Shell.service"
    assert sum("mobi.phosh.Shell" in line for line in out) == 1


def test_launch_escapes_dashes_in_the_scope_name(mod, apply, capsys):
    mod.launch("/x/org.my-app.desktop", "org.my-app", 3)
    out = capsys.readouterr().out
    assert f"--unit=app-gnome-org.my\\x2dapp-{os.getpid()}3.scope" in out


# --- Reading /proc and the cgroup tree ---------------------------------------------------------

def test_read_and_read_link(mod, tmp_path):
    (tmp_path / "f").write_bytes(b"abc\0def")
    assert mod.read(str(tmp_path / "f")) == "abc\0def"
    assert mod.read(str(tmp_path / "f"), "rb") == b"abc\0def"
    assert mod.read(str(tmp_path / "missing")) is None
    (tmp_path / "exe").symlink_to("/usr/bin/phosh (deleted)")
    assert mod.read_link(str(tmp_path / "exe")) == "/usr/bin/phosh (deleted)"
    assert mod.read_link(str(tmp_path / "f")) is None  # not a link


def test_boot_and_start_time(mod, proc):
    files, _links = proc
    files["/proc/stat"] = "cpu 1 2 3\nintr 0\n"
    assert mod.boot_time() == 0
    files["/proc/stat"] = "cpu 1\nbtime 1700000000\nprocesses 5\n"
    assert mod.boot_time() == 1700000000
    assert mod.start_time(42, 1000) is None  # gone
    files["/proc/42/stat"] = stat_line(42, "a (b) c", 1, start=os.sysconf("SC_CLK_TCK") * 30)
    assert mod.start_time(42, 1000) == 1030


def test_stale_reason_of_an_updated_flatpak(mod, proc, tmp_path):
    files, links = proc
    runtime = tmp_path / "runtime" / "org.gnome.Platform" / "aarch64" / "49"
    (runtime / "old").mkdir(parents=True)
    (runtime / "new").mkdir()
    (runtime / "active").symlink_to("new")
    links["/proc/8/exe"] = "/app/bin/maps"
    files["/proc/8/maps"] = maps(("r-xp", "/app/bin/maps"), ("r--p", "/usr/share/x (deleted)"))
    files["/proc/8/root/.flatpak-info"] = f"[Instance]\napp-path=\nruntime-path={runtime}/old/files\n"
    assert mod.stale_reason(8, 0) == "runtime updated"
    # A deployment without "active" (removed meanwhile) says nothing
    files["/proc/8/root/.flatpak-info"] = f"[Instance]\nruntime-path={tmp_path}/gone/x/files\n"
    assert mod.stale_reason(8, 0) is None


class OsProxy:
    def __init__(self, real, **overrides):
        self._real = real
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_stale_reason_script_arguments(mod, proc, monkeypatch):
    files, links = proc
    links["/proc/9/exe"] = "/usr/bin/bash"
    files["/proc/9/stat"] = stat_line(9, "bash", 1, start=0)
    # No arguments at all, and a script outside /usr: nothing to compare
    files["/proc/9/cmdline"] = "bash\0"
    assert mod.stale_reason(9, 0) is None
    files["/proc/9/cmdline"] = "bash\0-l\0-i\0"  # an interactive shell: only options
    assert mod.stale_reason(9, 0) is None
    files["/proc/9/cmdline"] = "bash\0-e\0/home/u/run.sh\0/usr/bin/env\0"
    assert mod.stale_reason(9, 0) is None
    # Only the first argument that is no option is the script
    files["/proc/9/cmdline"] = "bash\0/usr/bin/env\0"
    assert mod.stale_reason(9, 0) == "/usr/bin/env"
    # Its start time unknown (gone meanwhile): not counted
    del files["/proc/9/stat"]
    assert mod.stale_reason(9, 0) is None
    # The script removed between the check and its stat
    files["/proc/9/stat"] = stat_line(9, "bash", 1, start=0)

    def stat(path, *args, **kwargs):
        if path == "/usr/bin/env":
            raise FileNotFoundError(path)
        return os.stat(path, *args, **kwargs)
    monkeypatch.setattr(mod, "os", OsProxy(os, stat=stat))
    assert mod.stale_reason(9, 0) is None


def test_unit_digests_skip_unreadable_files(mod, tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "a.service").write_text("A")
    (tmp_path / "s" / "secret.service").write_text("S")
    (tmp_path / "s" / "secret.service").chmod(0)
    monkeypatch.setattr(mod, "UNIT_DIRS", {"system": str(tmp_path / "s")})
    assert list(mod.unit_digests()) == [str(tmp_path / "s" / "a.service")]


@pytest.fixture
def cgroups(mod, tmp_path, monkeypatch):
    """/sys/fs/cgroup as a directory in tmp_path."""
    top = tmp_path / "cgroup"

    def moved(path):
        return str(path).replace("/sys/fs/cgroup", str(top), 1)
    path = OsProxy(os.path, isdir=lambda p: os.path.isdir(moved(p)))
    monkeypatch.setattr(mod, "os", OsProxy(os, path=path, listdir=lambda p: os.listdir(moved(p))))
    return top


def test_session_uids_and_running_user_units(mod, cgroups):
    assert mod.session_uids() == []  # no cgroup v2 tree
    user = cgroups / "user.slice"
    (user / "user-1000.slice" / "user@1000.service" / "app.slice" / "feedbackd.service").mkdir(parents=True)
    (user / "user-1000.slice" / "user@1000.service" / "session.slice" / "mobi.phosh.Shell.service").mkdir(parents=True)
    (user / "user-32012.slice").mkdir()
    (user / "user.slice-not-a-user").mkdir()
    (user / "cgroup.procs").write_text("")
    assert sorted(mod.session_uids()) == [1000, 32012]
    assert mod.user_unit_running(1000, "feedbackd.service")
    assert mod.user_unit_running(1000, "mobi.phosh.Shell.service")
    assert not mod.user_unit_running(1000, "gone.service")
    assert not mod.user_unit_running(32012, "feedbackd.service")


def test_check_names_a_reason_once_and_skips_vanished_processes(mod, system, proc, monkeypatch):
    files, _links = proc
    system(200, "nm", "/system.slice/NetworkManager.service")
    system(201, "nm", "/system.slice/NetworkManager.service")
    system(202, "gone", "/system.slice/x.service")
    del files["/proc/202/stat"]  # ended while the list was read
    assert mod.check()["reboot"] == ["system: NetworkManager.service (nm) uses /usr/bin/x"]


def test_check_apps_without_a_launcher(mod, system):
    # The session names it like an app, but no desktop file exists: a scope cannot be started
    # again, a service restarts like any other
    system(100, "tool", f"{USER}/app.slice/app-gnome-org.no.launcher-3.scope")
    result = mod.check()
    assert result["reboot"] == ["session: app-gnome-org.no.launcher-3.scope (tool) uses /usr/bin/x"]


def test_check_app_service_without_a_launcher_is_restarted(mod, system):
    system(100, "svc", f"{USER}/app.slice/app-gnome-org.no.launcher@1.service")
    result = mod.check()
    assert result["level"] == "ui" and result["reboot"] == []
    assert result["units"] == [{"unit": "app-gnome-org.no.launcher@1.service", "uid": UID, "kind": "service",
                                "desktop": None, "reasons": ["svc uses /usr/bin/x"]}]


# --- apply for real (systemctl and systemd-run are stubs) --------------------------------------

@pytest.fixture
def user_manager(mod, fake_bin, tmp_path, monkeypatch):
    """systemctl --user as a stub: a unit is active while <dir>/<unit> exists, "stop" removes it
    unless the unit is in <dir>/stuck/."""
    units = tmp_path / "units"
    (units / "stuck").mkdir(parents=True)
    fake_bin.stub("systemctl", f'''
shift 2  # --user --quiet
case "$1" in
  is-active) [ -e "{units}/$2" ];;
  show) echo "RefuseManualStop=no"; echo "Restart=no";;
  stop) unit="${{@: -1}}"; [ -e "{units}/stuck/$unit" ] || rm -f "{units}/$unit";;
  *) exit 0;;
esac''')
    fake_bin.stub("systemd-run")
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    clock = {"now": 0.0, "slept": 0.0}

    def sleep(s):
        clock["now"] += s
        clock["slept"] += s
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=sleep))
    monkeypatch.setattr(mod, "DRY_RUN", False)
    return units, clock


def test_wait_inactive(mod, user_manager):
    units, clock = user_manager
    (units / "a.service").touch()
    assert mod.unit_active("a.service")
    assert mod.wait_inactive("a.service", 1) is False
    assert clock["now"] == pytest.approx(1.0)
    (units / "a.service").unlink()
    assert not mod.unit_active("a.service") and mod.wait_inactive("a.service", 1) is True


def test_apply_for_real_stops_kills_and_launches(mod, user_manager, fake_bin, tmp_path):
    units, clock = user_manager
    for name in ("app-gnome-org.a-1.scope", "app-gnome-org.stuck-1.scope", "feedbackd.service"):
        (units / name).touch()
    (units / "stuck" / "app-gnome-org.stuck-1.scope").touch()
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"apps": [app_entry("org.a"), app_entry("org.stuck")],
                                  "units": [unit("feedbackd.service")]}))
    mod.apply("ui", str(result))
    calls = [c[2:] for c in fake_bin.calls("systemctl") if c[2] != "is-active"]
    assert calls == [
        ["show", "-p", "RefuseManualStop,Restart", "feedbackd.service"],
        ["restart", "feedbackd.service"],
        ["stop", "--no-block", "app-gnome-org.a-1.scope"],
        ["stop", "--no-block", "app-gnome-org.stuck-1.scope"],
        # Did not stop within 10 s
        ["kill", "--signal=SIGKILL", "app-gnome-org.stuck-1.scope"],
    ]
    assert 12 <= clock["slept"] < 12.5  # 10 s for the stop, 2 s after the kill
    # Both apps are opened again, each in a scope of its own
    deadline = time.monotonic() + 10
    while len(fake_bin.calls("systemd-run")) < 2:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    pid = os.getpid()
    assert sorted(fake_bin.calls("systemd-run")) == [
        ["--user", "--scope", "--collect", "--quiet", f"--unit=app-gnome-org.a-{pid}0.scope", "--",
         "gio", "launch", "/usr/share/applications/org.a.desktop"],
        ["--user", "--scope", "--collect", "--quiet", f"--unit=app-gnome-org.stuck-{pid}1.scope", "--",
         "gio", "launch", "/usr/share/applications/org.stuck.desktop"]]


# --- The command line --------------------------------------------------------------------------

def test_main_snapshot_and_check(mod, system, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "UNIT_DIRS", {"system": str(tmp_path / "s"), "user": str(tmp_path / "u")})
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "a.service").write_text("A")
    monkeypatch.setattr("sys.argv", ["plinius-restart", "snapshot"])
    mod.main()
    snapshot = json.loads(capsys.readouterr().out)
    assert list(snapshot) == [str(tmp_path / "s" / "a.service")]
    system(100, "calc", f"{USER}/app.slice/app-gnome-org.gnome.Calculator-1.scope")
    monkeypatch.setattr("sys.argv", ["plinius-restart", "check"])
    mod.main()
    assert json.loads(capsys.readouterr().out)["level"] == "apps"


def test_program_apply_dry_run(fake_bin, tmp_path):
    fake_bin.stub("systemctl", 'case "$3" in is-active) exit 0;; show) echo RefuseManualStop=no;; esac')
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"apps": [app_entry("org.a")], "units": [unit("feedbackd.service")]}))
    r = subprocess.run([sys.executable, "-I", str(SCRIPT), "apply", "apps", str(result), "--dry-run"],
                       env=fake_bin.env(isolated=True), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = r.stdout.splitlines()
    # "apps": the units are left alone
    assert out[0] == "systemctl --user stop --no-block app-gnome-org.a-1.scope"
    assert out[1].endswith("gio launch /usr/share/applications/org.a.desktop") and len(out) == 2
    # Only the is-active questions reached systemctl
    assert {c[2] for c in fake_bin.calls("systemctl")} == {"is-active"}
    r = subprocess.run([sys.executable, "-I", str(SCRIPT), "apply", "everything", str(result)],
                       env=fake_bin.env(isolated=True), capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "invalid choice" in r.stderr
