# waydroid-plinius-screen: Android's screen follows the phone's lock, its status icons and time
# zone follow the phone's, its keyboard layout, clock, style and language Phosh's. The logind and
# timedated proxies are stand-ins (cached properties, the g-properties-changed handler); waydroid
# is a stub whose status comes from FAKE_STATE, whose Android answers from FAKE_* variables, and
# which logs its calls; setpriv answers gsettings from FAKE_GS_* variables.

import weakref

import pytest

from conftest import PKG, load_script

SCRIPT = PKG / "waydroid-plinius" / "waydroid-plinius-screen"

WAYDROID = r'''
if [ "$1" = status ]; then
	[ -n "$FAKE_STATUS_FAILS" ] && exit 1
	printf 'Session:\tRUNNING\nContainer:\t%s\nVendor type:\tHALIUM_14\n' "$FAKE_STATE"
fi
if [ "$1" = shell ]; then
	[ -n "$FAKE_SHELL_FAILS" ] && exit 1
	case "$5" in
	"getprop sys.boot_completed") echo "${FAKE_BOOTED-1}" ;;
	*persist.sys.timezone*)
		[ -n "$FAKE_QUERY_FAILS" ] && exit 1
		printf 'icons=%s\nzone=%s\n' "${FAKE_ICONS-null}" "${FAKE_ZONE-GMT}"
		printf 'api=%s\nuser=%s\nclock=%s\nnight=Night mode: %s\nlocale=%s\n' "${FAKE_API-33}" \
			"${FAKE_USER-}" "${FAKE_CLOCK-null}" "${FAKE_NIGHT-no}" "${FAKE_LOCALE-}" ;;
	"dumpsys input")
		[ -n "$FAKE_DUMP_FAILS" ] && exit 1
		printf '%b' "$FAKE_DUMP" ;;
	"service call input 17 "*)
		case "$5" in
		*"$FAKE_LAYOUT_OF"*) printf '%b' "$FAKE_CURRENT" ;;
		*) echo "Result: Parcel(00000000 ffffffff   '........')" ;;
		esac ;;
	*) [ -n "$FAKE_APPLY_FAILS" ] && exit 1 ;;
	esac
fi
exit 0
'''

gi = pytest.importorskip("gi")
from gi.repository import GLib  # noqa: E402


class Proxy:
    """A Gio.DBusProxy stand-in: cached properties and its g-properties-changed handler, held
    weakly: like a real proxy, it does not keep its owner alive."""

    def __init__(self, path, props):
        self.path = path
        self.props = dict(props)
        self.handler = None

    def connect(self, signal, handler):
        assert signal == "g-properties-changed"
        self.handler = weakref.WeakMethod(handler)

    def get_cached_property(self, name):
        return self.props.get(name)

    def change(self, **props):
        self.props.update(props)
        handler = self.handler()
        if handler:
            handler(self, GLib.Variant("a{sv}", props), [])


@pytest.fixture
def screen(fake_bin, monkeypatch, tmp_path):
    mod = load_script(SCRIPT)
    fake_bin.stub("waydroid", WAYDROID)
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    monkeypatch.setenv("FAKE_STATE", "RUNNING")
    # The phone's time zone: a localtime link into a zoneinfo tree of its own
    zoneinfo = tmp_path / "zoneinfo"
    (zoneinfo / "Europe").mkdir(parents=True)
    (zoneinfo / "Europe" / "Berlin").write_text("TZif")
    (tmp_path / "localtime").symlink_to(zoneinfo / "Europe" / "Berlin")
    monkeypatch.setattr(mod, "LOCALTIME", str(tmp_path / "localtime"))
    monkeypatch.setattr(mod, "ZONEINFO", str(zoneinfo) + "/")
    proxies = {mod.TIMEDATE_PATH: Proxy(mod.TIMEDATE_PATH, {})}

    def new_sync(bus, flags, info, name, path, iface, cancel):
        assert name == ("org.freedesktop.timedate1" if path == mod.TIMEDATE_PATH else "org.freedesktop.login1")
        return proxies[path]

    monkeypatch.setattr(mod.Gio.DBusProxy, "new_sync", new_sync)
    mod.proxies = proxies
    return mod


def seat(mod, session="/org/freedesktop/login1/session/_31"):
    mod.proxies[mod.SEAT] = Proxy(mod.SEAT, {"ActiveSession": GLib.Variant("(so)", ("1", session))})
    return mod.proxies[mod.SEAT]


def session(mod, locked, path="/org/freedesktop/login1/session/_31"):
    mod.proxies[path] = Proxy(path, {"LockedHint": GLib.Variant("b", locked)})
    return mod.proxies[path]


def keyevents(fake_bin):
    return [c[-1] for c in fake_bin.calls("waydroid") if c[0] == "shell" and "keyevent" in c]


def scripts(fake_bin):
    """The scripts that changed Android's settings (not the ones that only asked)."""
    queries = ("getprop", "dumpsys input", "service call input 17 ")
    return [c[-1] for c in fake_bin.calls("waydroid")
            if c[:4] == ["shell", "--", "sh", "-c"] and not any(q in c[-1] for q in queries)]


def test_lock_and_unlock(screen, fake_bin, capsys):
    seat(screen)
    s = session(screen, False)
    w = screen.Watcher(None)  # noqa: F841 (held: its handlers live with it)
    # Unlocked at start: an Android that runs is awake already
    assert keyevents(fake_bin) == []
    s.change(LockedHint=GLib.Variant("b", True))
    s.change(LockedHint=GLib.Variant("b", True))
    s.change(LockedHint=GLib.Variant("b", False))
    assert keyevents(fake_bin) == ["KEYCODE_SLEEP", "KEYCODE_WAKEUP"]
    assert ["shell", "--", "input", "keyevent", "KEYCODE_SLEEP"] in fake_bin.calls("waydroid")
    out = capsys.readouterr().out
    assert "Phone locked: Android asleep" in out and "Phone unlocked: Android awake" in out


def test_locked_at_start_puts_android_to_sleep(screen, fake_bin):
    seat(screen)
    session(screen, True)
    w = screen.Watcher(None)  # noqa: F841 (held: its handlers live with it)
    assert keyevents(fake_bin) == ["KEYCODE_SLEEP"]


def test_unlock_thaws_a_frozen_container_first(screen, fake_bin, monkeypatch):
    seat(screen)
    s = session(screen, True)
    w = screen.Watcher(None)  # noqa: F841 (held: its handlers live with it)
    monkeypatch.setenv("FAKE_STATE", "FROZEN")
    s.change(LockedHint=GLib.Variant("b", False))
    calls = fake_bin.calls("waydroid")
    assert calls[-2:] == [["container", "unfreeze"], ["shell", "--", "input", "keyevent", "KEYCODE_WAKEUP"]]


def test_lock_of_a_frozen_container_does_not_thaw_it(screen, fake_bin, monkeypatch):
    monkeypatch.setenv("FAKE_STATE", "FROZEN")
    assert screen.apply(True)
    assert ["container", "unfreeze"] not in fake_bin.calls("waydroid")


@pytest.mark.parametrize("env", [{"FAKE_STATE": "STOPPED"}, {"FAKE_STATUS_FAILS": "1", "FAKE_STATE": ""}])
def test_without_android_nothing_happens(screen, fake_bin, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert screen.apply(True) is False
    assert keyevents(fake_bin) == []


def test_failed_keyevent_is_reported(screen, fake_bin, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_SHELL_FAILS", "1")
    assert screen.apply(True) is False
    assert "(failed)" in capsys.readouterr().out


def test_waydroid_missing(screen, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert screen.container_state() is None
    assert screen.run(["waydroid", "shell"]) is False


def test_waydroid_hanging(screen, monkeypatch):
    import subprocess

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired("waydroid", 20)
    monkeypatch.setattr(screen.subprocess, "run", hang)
    assert screen.container_state() is None
    assert screen.run(["waydroid", "shell"]) is False


def test_another_session_becomes_active(screen, fake_bin):
    st = seat(screen)
    session(screen, False)
    w = screen.Watcher(None)  # noqa: F841 (held: its handlers live with it)
    other = session(screen, True, "/org/freedesktop/login1/session/_32")
    st.change(ActiveSession=GLib.Variant("(so)", ("2", other.path)))
    assert keyevents(fake_bin) == ["KEYCODE_SLEEP"]
    # A change of another property of the seat or the session changes nothing
    st.change(CanGraphical=GLib.Variant("b", True))
    other.change(Active=GLib.Variant("b", True))
    assert keyevents(fake_bin) == ["KEYCODE_SLEEP"]


def test_no_active_session(screen, fake_bin):
    st = seat(screen, "/")
    w = screen.Watcher(None)
    assert w.session is None
    st.props["ActiveSession"] = None
    w.follow_session()
    assert w.session is None and keyevents(fake_bin) == []


def test_session_without_locked_hint(screen, fake_bin):
    seat(screen)
    screen.proxies["/org/freedesktop/login1/session/_31"] = Proxy("/org/freedesktop/login1/session/_31", {})
    w = screen.Watcher(None)
    assert w.locked is False and keyevents(fake_bin) == []


def test_main_without_system_bus(screen, monkeypatch, capsys):
    def no_bus(kind, cancel):
        raise GLib.Error("no bus")
    monkeypatch.setattr(screen.Gio, "bus_get_sync", no_bus)
    assert screen.main() == 1
    assert "Cannot watch the phone: no bus" in capsys.readouterr().err


def test_main_keeps_the_watcher_while_the_loop_runs(screen, fake_bin, monkeypatch):
    """The lock is still followed inside the loop: the watcher (and its proxies' handlers) must
    outlive the call that made it."""
    import gc
    seat(screen)
    s = session(screen, False)
    monkeypatch.setattr(screen.Gio, "bus_get_sync", lambda kind, cancel: None)

    def loop_run(self):
        gc.collect()
        s.change(LockedHint=GLib.Variant("b", True))
    monkeypatch.setattr(screen.GLib, "MainLoop", lambda: type("L", (), {"run": loop_run})())
    assert screen.main() == 0
    assert keyevents(fake_bin) == ["KEYCODE_SLEEP"]
    # Android's settings were applied as the service came up
    assert len(scripts(fake_bin)) == 1



# ---- Android's settings as the phone's ----

def test_settings_of_a_new_android(screen, fake_bin, capsys):
    # Without a session user Android gets the phone's settings only
    assert screen.apply_settings() == ""
    assert scripts(fake_bin) == [
        "settings put secure icon_blacklist " + screen.STATUS_ICONS + "; "
        "cmd time_zone_detector set_auto_detection_enabled false; "
        "cmd time_zone_detector suggest_manual_time_zone --zone_id Europe/Berlin"]
    out = capsys.readouterr().out
    assert "Android's settings: no status icons of the phone's top bar, " \
        "time zone Europe/Berlin" in out


def test_settings_already_there_stay(screen, fake_bin, monkeypatch, capsys):
    # The user's own icon list, the phone's zone already in Android
    monkeypatch.setenv("FAKE_ICONS", "rotate")
    monkeypatch.setenv("FAKE_ZONE", "Europe/Berlin")
    assert screen.apply_settings() == ""
    assert scripts(fake_bin) == [] and capsys.readouterr().out == ""


def test_only_the_time_zone_changes(screen, fake_bin, monkeypatch):
    monkeypatch.setenv("FAKE_ICONS", screen.STATUS_ICONS)
    assert screen.apply_settings() == ""
    assert scripts(fake_bin) == ["cmd time_zone_detector set_auto_detection_enabled false; "
                                 "cmd time_zone_detector suggest_manual_time_zone --zone_id Europe/Berlin"]


def test_failed_settings_are_reported(screen, fake_bin, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_APPLY_FAILS", "1")
    assert screen.apply_settings() == ""
    assert capsys.readouterr().out.rstrip().endswith("(failed)")


@pytest.mark.parametrize("env", [{"FAKE_STATE": "FROZEN"}, {"FAKE_STATE": "STOPPED"}, {"FAKE_BOOTED": ""},
                                 {"FAKE_SHELL_FAILS": "1"}, {"FAKE_QUERY_FAILS": "1"}],
                         ids=["frozen", "stopped", "booting", "no-shell", "query-fails"])
def test_settings_wait_for_a_booted_android(screen, fake_bin, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert screen.apply_settings() is None
    assert scripts(fake_bin) == []


def test_phone_zone(screen, tmp_path, monkeypatch):
    assert screen.phone_zone() == "Europe/Berlin"
    # Outside the zoneinfo tree (UTC as a file of its own), or a name unfit for a shell
    (tmp_path / "plain").write_text("TZif")
    monkeypatch.setattr(screen, "LOCALTIME", str(tmp_path / "plain"))
    assert screen.phone_zone() is None
    odd = tmp_path / "zoneinfo" / "Bad;zone"
    odd.write_text("TZif")
    monkeypatch.setattr(screen, "LOCALTIME", str(odd))
    assert screen.phone_zone() is None
    monkeypatch.setattr(screen.os.path, "realpath", lambda p: (_ for _ in ()).throw(OSError()))
    assert screen.phone_zone() is None


def test_android_shell_missing_or_hanging(screen, monkeypatch):
    import subprocess
    monkeypatch.setenv("PATH", "/nonexistent")
    assert screen.android("true") is None

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired("waydroid", 20)
    monkeypatch.setattr(screen.subprocess, "run", hang)
    assert screen.android("true") is None


def test_settings_wait_for_android_and_follow_the_time_zone(screen, monkeypatch):
    results = [None, None, ""]
    calls = []

    def apply_fn():
        calls.append(1)
        return results.pop(0) if results else ""
    timeouts = []
    monkeypatch.setattr(screen.GLib, "timeout_add_seconds",
                        lambda secs, fn: timeouts.append((secs, fn)) or 7)
    st = screen.Settings(None, apply_fn)
    # Not booted yet: looked at again every BOOT_POLL seconds until it is
    assert timeouts == [(screen.BOOT_POLL, st.on_poll)] and st.source == 7
    assert st.on_poll() == screen.GLib.SOURCE_CONTINUE
    # A new zone while it still waits: the poll is there already
    td = screen.proxies[screen.TIMEDATE_PATH]
    td.change(Timezone=GLib.Variant("s", "Europe/Paris"))
    assert len(calls) == 2
    assert st.on_poll() == screen.GLib.SOURCE_REMOVE and st.source is None
    # Booted: a new zone is applied at once, another property changes nothing
    td.change(Timezone=GLib.Variant("s", "Europe/Berlin"))
    td.change(NTP=GLib.Variant("b", True))
    assert len(calls) == 4 and len(timeouts) == 1
    # timedated may only invalidate the property
    td.handler()(td, GLib.Variant("a{sv}", {}), ["Timezone"])
    assert len(calls) == 5
    # Without a session user nothing is watched
    assert st.monitors == []


# ---- Phosh's settings of the session user ----

SETPRIV = r'''
[ -n "$FAKE_SETPRIV_FAILS" ] && exit 1
key="${@: -1}"
var="FAKE_GS_$(echo "$key" | tr 'a-z-' 'A-Z_')"
[ -z "${!var+x}" ] && exit 1
printf '%s\n' "${!var}"
'''

KEYBOARD_DUMP = (r"Event Hub State:\n  BuiltInKeyboardId: -2\n  Devices:\n"
                 r"    -1: Virtual\n      Classes: KEYBOARD | ALPHAKEY | VIRTUAL\n      Descriptor: 1111\n"
                 r"      Identifier: bus=0x0000, vendor=0x0000, product=0x0000, version=0x0000\n"
                 r"    1: wayland_touch\n      Classes: TOUCH\n      Descriptor: 2222\n"
                 r"      Identifier: bus=0x0006, vendor=0x0001, product=0x0003, version=0x0001\n"
                 r"    2: wayland_keyboard\n      Classes: KEYBOARD | ALPHAKEY | DPAD\n"
                 r"      Descriptor: 248b7bc7\n"
                 r"      Identifier: bus=0x0006, vendor=0x0001, product=0x0001, version=0x0001\n"
                 r"Input Reader State:\n  Device 3: wayland_keyboard\n      KeyboardType: 2\n"
                 r"    2: wayland_keyboard\n      Classes: KEYBOARD | ALPHAKEY | DPAD\n"
                 r"      Descriptor: 248b7bc7\n"
                 r"      Identifier: bus=0x0006, vendor=0x0001, product=0x0001, version=0x0001\n"
                 r"    4: bt_keyboard\n      Classes: KEYBOARD | ALPHAKEY\n      Descriptor: 3333\n"
                 r"      Identifier: bus=0x0005, vendor=0x04e8, product=0x7021, version=0x0001\n")

CURRENT_GERMAN = (r"Result: Parcel(\n  0x00000000: 00000000 00000060 006f0063 002e006d '....`...c.o.m...'\n"
                  r"  0x00000010: 006b0065 00620079 0061006f 00640072 '..keyboard_layout_ger'\n"
                  r"  0x00000020: 006b0065 00620079 0061006f 00640072 'man.....'\n)\n")


@pytest.fixture
def phosh(screen, fake_bin, monkeypatch, tmp_path):
    """A session user (plinius, uid 32011) whose settings of Phosh come from FAKE_GS_*."""
    fake_bin.stub("setpriv", SETPRIV)
    home = tmp_path / "home"
    (home / ".config" / "dconf").mkdir(parents=True)
    (home / ".config" / "dconf" / "user").write_text("")
    accounts = tmp_path / "accounts"
    accounts.mkdir()
    (accounts / "plinius").write_text("[User]\nLanguages=de_DE.UTF-8;\nSession=\n")
    monkeypatch.setattr(screen, "ACCOUNTS", str(accounts))
    monkeypatch.setattr(screen, "LOCALE_CONF", str(tmp_path / "locale.conf"))

    class Pw:
        pw_uid = 32011
        pw_gid = 32012
        pw_dir = str(home)

    def getpwnam(name):
        if name != "plinius":
            raise KeyError(name)
        return Pw
    monkeypatch.setattr(screen.pwd, "getpwnam", getpwnam)
    for k, v in {"FAKE_USER": "plinius", "FAKE_ZONE": "Europe/Berlin",
                 "FAKE_ICONS": "x", "FAKE_DUMP": KEYBOARD_DUMP, "FAKE_LAYOUT_OF": "3333",
                 "FAKE_CURRENT": CURRENT_GERMAN,
                 "FAKE_GS_CLOCK_FORMAT": "'24h'", "FAKE_GS_COLOR_SCHEME": "'prefer-dark'",
                 "FAKE_GS_SOURCES": "[('xkb', 'de+nodeadkeys'), ('xkb', 'us')]"}.items():
        monkeypatch.setenv(k, v)
    screen.home = home
    screen.accounts = accounts
    return screen


def test_phosh_settings_go_to_android(phosh, fake_bin, capsys):
    assert phosh.apply_settings() == "plinius"
    layout = phosh.INPUT_LAYOUT + "german"
    assert scripts(fake_bin) == [
        "settings put system time_12_24 24; cmd uimode night yes; "
        # The Wayland keyboard (once), not the virtual one nor the one that has German already
        "service call input 18 i32 1 s16 248b7bc7 i32 1 i32 1 s16 " + layout + "; "
        "setprop persist.sys.locale de-DE"]
    out = capsys.readouterr().out
    assert "24-hour clock, dark style, keyboard layout german, language de-DE (from Android's next start)" in out
    # Read as the user, with the user's session bus
    setpriv = fake_bin.calls("setpriv")[0]
    assert setpriv[:3] == ["--reuid=32011", "--regid=32012", "--init-groups"]
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/32011/bus" in setpriv
    assert setpriv[-3:] == ["get", "org.gnome.desktop.interface", "clock-format"]


def test_phosh_settings_already_in_android(phosh, fake_bin, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLOCK", "24")
    monkeypatch.setenv("FAKE_NIGHT", "yes")
    monkeypatch.setenv("FAKE_LOCALE", "de-DE")
    monkeypatch.setenv("FAKE_LAYOUT_OF", "i32")
    assert phosh.apply_settings() == "plinius"
    assert scripts(fake_bin) == [] and capsys.readouterr().out == ""


def test_light_style_and_12_hours(phosh, fake_bin, monkeypatch):
    monkeypatch.setenv("FAKE_GS_CLOCK_FORMAT", "'12h'")
    monkeypatch.setenv("FAKE_GS_COLOR_SCHEME", "'default'")
    monkeypatch.setenv("FAKE_NIGHT", "yes")
    monkeypatch.setenv("FAKE_GS_SOURCES", "[('ibus', 'anthy')]")
    monkeypatch.setenv("FAKE_LOCALE", "de-DE")
    assert phosh.apply_settings() == "plinius"
    assert scripts(fake_bin) == ["settings put system time_12_24 12; cmd uimode night no"]


@pytest.mark.parametrize("env", [{"FAKE_SETPRIV_FAILS": "1"},
                                 {"FAKE_GS_CLOCK_FORMAT": "not a variant", "FAKE_GS_COLOR_SCHEME": "(",
                                  "FAKE_GS_SOURCES": "["}],
                         ids=["gsettings-fails", "unparsable"])
def test_phosh_settings_unreadable(phosh, fake_bin, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FAKE_LOCALE", "de-DE")
    assert phosh.apply_settings() == "plinius"
    assert scripts(fake_bin) == []


def test_phosh_settings_of_a_user_unknown_here(phosh, fake_bin, monkeypatch):
    # Known to Android, not to the phone; or a name unfit for a shell
    monkeypatch.setenv("FAKE_USER", "nobody2")
    monkeypatch.setenv("FAKE_LOCALE", "de-DE")
    monkeypatch.setenv("LANG_UNUSED", "")
    assert phosh.apply_settings() == "nobody2"
    assert scripts(fake_bin) == []
    monkeypatch.setenv("FAKE_USER", "a;b")
    assert phosh.apply_settings() == ""


def test_setpriv_missing_or_hanging(phosh, monkeypatch):
    import subprocess
    monkeypatch.setenv("PATH", "/nonexistent")
    assert phosh.phosh_setting("plinius", "s", "k") is None

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired("setpriv", 20)
    monkeypatch.setattr(phosh.subprocess, "run", hang)
    assert phosh.phosh_setting("plinius", "s", "k") is None


@pytest.mark.parametrize("env", [{"FAKE_API": "35"}, {"FAKE_API": "x"}, {"FAKE_DUMP_FAILS": "1"}],
                         ids=["unknown-android", "no-api", "dumpsys-fails"])
def test_no_keyboard_layout_without_the_way_to_set_it(phosh, fake_bin, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FAKE_CLOCK", "24")
    monkeypatch.setenv("FAKE_NIGHT", "yes")
    monkeypatch.setenv("FAKE_LOCALE", "de-DE")
    assert phosh.apply_settings() == "plinius"
    assert scripts(fake_bin) == []


@pytest.mark.parametrize("sources,layout", [
    ([("xkb", "us")], "english_us"), ([("xkb", "us+dvorak")], "english_us_dvorak"),
    ([("xkb", "ch+fr")], "swiss_french"), ([("xkb", "de+nodeadkeys")], "german"),
    ([("ibus", "anthy"), ("xkb", "fr")], "french"), ([("xkb", "nl")], None), ([], None), (None, None)])
def test_keyboard_layout(screen, sources, layout):
    assert screen.keyboard_layout(sources) == layout


def test_user_locale(phosh, tmp_path):
    assert phosh.user_locale("plinius") == "de-DE"
    (phosh.accounts / "plinius").write_text("[User]\nLanguage=pt_BR.UTF-8\n")
    assert phosh.user_locale("plinius") == "pt-BR"
    # Without a language of the user's own: the system's
    (phosh.accounts / "plinius").write_text("[User]\nLanguages=;\n")
    (tmp_path / "locale.conf").write_text('# system\nLANG="fr_FR.UTF-8"\n')
    assert phosh.user_locale("plinius") == "fr-FR"
    assert phosh.user_locale("nobody") == "fr-FR"
    (tmp_path / "locale.conf").write_text("LANG=C.UTF-8\n")
    assert phosh.user_locale("nobody") is None
    (tmp_path / "locale.conf").unlink()
    assert phosh.user_locale("nobody") is None


def settle():
    """Runs the default main context a while, for file monitors to report."""
    import time
    ctx = GLib.MainContext.default()
    for _ in range(50):
        while ctx.iteration(False):
            pass
        time.sleep(0.01)


def test_settings_follow_the_user_s_files(phosh):
    users = ["plinius"]
    calls = []

    def apply_fn():
        calls.append(1)
        return users[0]
    st = phosh.Settings(None, apply_fn)
    assert len(st.monitors) == 2
    first = st.monitors
    # dconf writes a new file for each change: Android gets the new settings
    new = phosh.home / ".config" / "dconf" / "user.new"
    new.write_text("changed")
    new.rename(phosh.home / ".config" / "dconf" / "user")
    settle()
    assert len(calls) >= 2
    # The same user again keeps the monitors; another user (unknown here) cancels them
    st.update()
    assert st.monitors is first
    users[0] = "nobody2"
    st.update()
    assert st.monitors == [] and all(m.is_cancelled() for m in first)
    users[0] = ""
    st.update()
    assert st.user == "" and st.monitors == []
    # Other events (a file deleted) change nothing
    count = len(calls)
    st.on_file_changed(None, None, None, phosh.Gio.FileMonitorEvent.DELETED)
    assert len(calls) == count


def test_settings_skip_a_file_that_cannot_be_watched(phosh, monkeypatch):
    real = phosh.Gio.File.new_for_path

    class Broken:
        def monitor_file(self, flags, cancel):
            raise GLib.Error("no inotify")

    monkeypatch.setattr(phosh.Gio.File, "new_for_path",
                        lambda path: Broken() if path.endswith("user") else real(path))
    st = phosh.Settings(None, lambda: "plinius")
    assert len(st.monitors) == 1
