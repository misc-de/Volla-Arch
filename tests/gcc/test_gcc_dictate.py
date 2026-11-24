# "Speech to Text" on the Keyboard page (cc-plinius-dictate.c): the switch is org.plinius.dictate
# enabled; without the speech model it asks pacman for the download size, asks the user, then
# installs plinius-dictate-models through a fake "pkexec plinius-pkg" and shows the progress from
# the package file growing in pacman's cache (in the program's root). Removing the model and the
# error answers of plinius-pkg too.

import pytest

from gcc_harness import (build_panel, call, click, dump, gcc_session, gcc_world,  # noqa: F401
                         set_active, settle, wait, wait_for)

MODEL = "/usr/share/plinius-dictate/parakeet-tdt-0.6b-v3-int8/encoder.int8.onnx"
PKG_TOOL = "/usr/lib/plinius-software/plinius-pkg"
CACHE = "/var/cache/pacman/pkg"

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-dictate.h"

static gboolean set_up;

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *sources = adw_preferences_group_new ();
    GtkWidget *other = adw_preferences_group_new ();

    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (sources), "Input Sources");
    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (other), "Special Character Entry");
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (sources));
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (other));
    set_up = cc_plinius_dictate_setup (t->page);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (!g_str_equal (name, "set_up"))
        return FALSE;
    json_builder_add_boolean_value (result, set_up);
    return TRUE;
}
'''

SUBTITLE = ("A microphone key next to the space bar of the on-screen keyboard types what you say into "
            "the text field. Speech is recognized on the phone; nothing is stored or sent. Needs about "
            "1 GB of memory while you dictate.")
SCHEMA = "org.plinius.dictate"


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    reps = [(f'"{p}', f'"{{root}}{p}') for p in (MODEL, PKG_TOOL, CACHE)]
    return build_panel(gcc_session, tmp_path_factory.mktemp("dictate"), "dictate",
                       ["cc-plinius-dictate.c"], GLUE, replacements={"cc-plinius-dictate.c": reps})


class Phone:
    def __init__(self, program, world):
        self.root = program.root
        self.world = world
        self.bin = world.fake_bin
        for d in list(self.root.iterdir()):
            import shutil
            shutil.rmtree(d) if d.is_dir() else d.unlink()

    def path(self, p):
        return self.root / p.lstrip("/")

    def model(self, installed=True):
        f = self.path(MODEL)
        if installed:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("onnx")
        elif f.exists():
            f.unlink()

    def cache(self, name, size):
        d = self.path(CACHE)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(b"\0" * size)

    def pacman(self, size_line="Download Size   : 1.50 MiB"):
        out = f"Name            : plinius-dictate-models\\n{size_line}\\nInstalled Size  : 640.00 MiB\\n"
        self.bin.stub("pacman", f'[ "$LC_ALL" = C ] || exit 7\nprintf "{out}"')

    def pkexec(self, code=0, output="", install=True, wait_for=None):
        """pkexec plinius-pkg: waits for the file wait_for (in tmp), installs or removes the model,
        exits with code."""
        model = self.path(MODEL)
        lines = []
        if wait_for:
            lines.append(f'for i in $(seq 1 400); do [ -e "{wait_for}" ] && break; sleep 0.05; done')
        if output:
            lines.append(f'printf "%s" "{output}"')
        if install:
            lines.append(f'if [ "$2" = install ]; then mkdir -p "{model.parent}"; echo x > "{model}"; fi')
            lines.append(f'if [ "$2" = remove ]; then rm -f "{model}"; fi')
        lines.append(f"exit {code}")
        self.bin.stub("pkexec", "\n".join(lines))

    def pkexec_calls(self):
        return self.bin.calls("pkexec")


@pytest.fixture
def phone(program, gcc_world):
    return Phone(program, gcc_world)


def group(screen):
    return screen.within(type="AdwPreferencesGroup", text="Speech to Text")


def switch(screen):
    return screen.one(type="AdwSwitchRow", text="Microphone Key")


def enabled(label="enabled"):
    return {"op": "gsettings_get", "schema": SCHEMA, "key": "enabled", "label": label}


def set_enabled(value):
    return {"op": "gsettings_set", "schema": SCHEMA, "key": "enabled", "value": "true" if value else "false"}


def test_group_below_the_input_sources(program, gcc_world, phone):
    r = gcc_world.run(program, [call("set_up"), dump("main")])
    assert r.value("set_up") is True
    s = r.dump("main")
    titles = [g["text"] for g in s.find(type="AdwPreferencesGroup")]
    assert titles == ["Input Sources", "Speech to Text", "Special Character Entry"]
    row = switch(s)
    assert row["subtitle"] == SUBTITLE and row["active"] is False
    # Not installed: nothing to remove, no progress
    assert not s.has(type="AdwButtonRow")
    assert not r.warnings()


def test_nothing_without_plinius_dictate(program, gcc_world, phone, tmp_path):
    # A schema directory without org.plinius.dictate
    import shutil
    import subprocess
    d = tmp_path / "schemas"
    d.mkdir()
    for f in gcc_world.session.schemas.glob("org.gnome.desktop.*.gschema.xml"):
        shutil.copy(f, d)
    subprocess.run(["glib-compile-schemas", str(d)], check=True, capture_output=True)
    gcc_world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(d)
    r = gcc_world.run(program, [call("set_up"), dump("main")])
    assert r.value("set_up") is False
    assert not r.dump("main").has(type="AdwPreferencesGroup", text="Speech to Text")


@pytest.mark.parametrize("on, installed, active, remove", [
    (False, False, False, False),
    (True, False, False, False),   # on without the model: the key could not work
    (False, True, False, True),
    (True, True, True, False),
])
def test_switch_and_remove_row_follow_setting_and_model(program, gcc_world, phone, on, installed, active,
                                                        remove):
    phone.model(installed)
    r = gcc_world.run(program, [set_enabled(on), settle(), dump("main")])
    s = r.dump("main")
    assert switch(s)["active"] is active
    assert s.has(type="AdwButtonRow", text="Remove Speech Model") is remove


def test_switch_with_the_model_sets_the_setting(program, gcc_world, phone):
    phone.model()
    r = gcc_world.run(program, [set_active(True, type="AdwSwitchRow"), settle(), enabled("on"), dump("on-screen"),
                                set_active(False, type="AdwSwitchRow"), settle(), enabled("off"),
                                dump("off-screen")])
    assert r.value("on") == "true"
    assert not r.dump("on-screen").has(type="AdwButtonRow")
    assert r.value("off") == "false"
    assert r.dump("off-screen").has(type="AdwButtonRow", text="Remove Speech Model")
    assert phone.pkexec_calls() == []


def test_setting_changed_elsewhere_moves_the_switch(program, gcc_world, phone):
    phone.model()
    r = gcc_world.run(program, [dump("before"), set_enabled(True), settle(), dump("after")])
    assert switch(r.dump("before"))["active"] is False
    assert switch(r.dump("after"))["active"] is True


@pytest.mark.parametrize("line, shown", [
    ("Download Size   : 1.50 MiB", "1.6\xa0MB"),
    ("Download Size   : 2.00 GiB", "2.1\xa0GB"),
    ("Download Size   : 512.00 KiB", "524.3\xa0kB"),
    ("Download Size   : 900 B", "900 bytes"),
])
def test_switch_without_the_model_asks_with_the_download_size(program, gcc_world, phone, line, shown):
    phone.pacman(line)
    r = gcc_world.run(program, [set_active(True, type="AdwSwitchRow"),
                                wait_for(type="AdwAlertDialog"), dump("asked"), enabled()])
    alert = r.dump("asked").one(type="AdwAlertDialog")
    assert alert["text"] == "Download Speech Model?"
    assert alert["subtitle"] == (
        f"Speech to text needs a speech model that is downloaded now ({shown}, about 640 MB once "
        "installed). Wi-Fi is best for this. The model recognizes 25 languages and works without an "
        "internet connection.")
    # The switch stays off until the model is there
    assert switch(r.dump("asked"))["active"] is False
    assert r.value("enabled") == "false"
    assert phone.bin.calls("pacman") == [["-Si", "plinius-dictate-models"]]


def test_cancel_downloads_nothing(program, gcc_world, phone):
    phone.pacman()
    phone.pkexec()
    r = gcc_world.run(program, [set_active(True, type="AdwSwitchRow"), wait_for(type="AdwAlertDialog"),
                                click(type="GtkButton", text="Cancel"), settle(), dump("main")])
    assert phone.pkexec_calls() == []
    assert not r.dump("main").has(type="AdwAlertDialog")


@pytest.mark.parametrize("line", ["Download Size   :", "Repository      : plinius"])
def test_model_not_in_the_database_says_update_first(program, gcc_world, phone, line):
    phone.pacman(line)
    r = gcc_world.run(program, [set_active(True, type="AdwSwitchRow"), wait_for(type="AdwAlertDialog"),
                                dump("asked")])
    alert = r.dump("asked").one(type="AdwAlertDialog")
    assert (alert["text"], alert["subtitle"]) == ("Speech Model Not Available",
                                                  "Update the system in Software first, then try again.")


def test_without_pacman_nothing_is_asked(program, gcc_world, phone):
    r = gcc_world.run(program, [set_active(True, type="AdwSwitchRow"), settle(200), dump("main")])
    assert not r.dump("main").has(type="AdwAlertDialog")
    assert switch(r.dump("main"))["active"] is False


def test_download_progress_and_install(program, gcc_world, phone):
    go = gcc_world.tmp / "go"
    phone.pacman("Download Size   : 1.00 MiB")
    phone.pkexec(wait_for=go)
    big = 1024 * 1024
    cache = phone.path(CACHE)
    cache.mkdir(parents=True)
    r = gcc_world.run(program, [
        set_active(True, type="AdwSwitchRow"), wait_for(type="AdwAlertDialog"),
        click(type="GtkButton", text="Download"), settle(), dump("preparing"),
        wait(700), dump("waiting"),
        {"op": "write_file", "path": str(cache / "plinius-dictate-models-1-1-any.pkg.tar.zst.part"),
         "text": "x" * (big // 4)},
        # Signatures and other packages do not count
        {"op": "write_file", "path": str(cache / "plinius-dictate-models-1-1-any.pkg.tar.zst.sig"),
         "text": "x" * big},
        {"op": "write_file", "path": str(cache / "plinius-dictate-models-1-1-any.pkg.tar.zst.sig.part"),
         "text": "x" * big},
        {"op": "write_file", "path": str(cache / "plinius-dictate-1-1-any.pkg.tar.zst"), "text": "x" * big},
        wait(700), dump("downloading"),
        {"op": "write_file", "path": str(cache / "plinius-dictate-models-1-1-any.pkg.tar.zst"),
         "text": "x" * big},
        wait(700), dump("installing"),
        {"op": "write_file", "path": str(go), "text": ""},
        wait_for(type="AdwActionRow", text="Installing Speech Model…", gone=True, timeout=10000), settle(300), dump("done"), enabled()])
    pre = r.dump("preparing")
    assert pre.one(type="AdwActionRow", text="Preparing Download…")
    assert switch(pre).get("sensitive") is False
    # No package file yet (the password dialog is up): still preparing, not "0 bytes of 1.0 MB"
    assert r.dump("waiting").one(type="AdwActionRow", text="Preparing Download…")
    assert not r.dump("waiting").has(type="AdwActionRow", text="Downloading Speech Model…")
    down = r.dump("downloading").one(type="AdwActionRow", text="Downloading Speech Model…")
    assert down["subtitle"] == "262.1\xa0kB of 1.0\xa0MB"
    bar = [n for n in down.walk() if n["type"] == "GtkProgressBar"][0]
    assert bar["fraction"] == pytest.approx(0.25)
    inst = r.dump("installing").one(type="AdwActionRow", text="Installing Speech Model…")
    assert not inst.get("subtitle")
    done = r.dump("done")
    assert switch(done)["active"] is True and switch(done).get("sensitive") is None
    assert not done.has(type="AdwActionRow", text="Installing Speech Model…")
    assert r.value("enabled") == "true"
    assert phone.pkexec_calls() == [[str(phone.path(PKG_TOOL)), "install", "plinius-dictate-models"]]
    assert not r.warnings()


def test_install_without_the_model_afterwards_leaves_it_off(program, gcc_world, phone):
    phone.pacman()
    phone.pkexec(install=False)
    r = gcc_world.run(program, [set_active(True, type="AdwSwitchRow"), wait_for(type="AdwAlertDialog"),
                                click(type="GtkButton", text="Download"), settle(500), dump("done"), enabled()])
    assert r.value("enabled") == "false"
    assert switch(r.dump("done"))["active"] is False
    assert not r.dump("done").has(type="AdwAlertDialog")


@pytest.mark.parametrize("code, message", [
    (3, "Update the system in Software first, then try again."),
    (4, "Update the system in Software first, then try again."),
    (9, "Another software installation is running. Try again when it is done."),
    (1, "Check the internet connection and try again."),
    (126, None),   # the password dialog closed
    (127, None),   # not authorized
])
def test_install_errors(program, gcc_world, phone, code, message):
    phone.pacman()
    phone.pkexec(code=code, output="error: something", install=False)
    r = gcc_world.run(program, [set_active(True, type="AdwSwitchRow"), wait_for(type="AdwAlertDialog"),
                                click(type="GtkButton", text="Download"),
                                wait_for(type="AdwActionRow", text="Preparing Download…", gone=True),
                                settle(200), dump("done")])
    s = r.dump("done")
    alerts = s.find(type="AdwAlertDialog")
    if message is None:
        assert alerts == []
    else:
        assert [(a["text"], a["subtitle"]) for a in alerts] == [("Speech Model Not Installed", message)]
    assert switch(s)["active"] is False and switch(s).get("sensitive") is None
    assert [m["message"] for m in r.warnings()] == [f"plinius-pkg ended with {code}: error: something"]


def test_without_pkexec_the_install_fails_at_once(program, gcc_world, phone):
    phone.pacman()
    r = gcc_world.run(program, [set_active(True, type="AdwSwitchRow"), wait_for(type="AdwAlertDialog"),
                                click(type="GtkButton", text="Download"), settle(200), dump("done")])
    alert = r.dump("done").one(type="AdwAlertDialog")
    assert (alert["text"], alert["subtitle"]) == ("Speech Model Not Installed",
                                                  "Check the internet connection and try again.")
    assert any(m["message"].startswith("Cannot run ") for m in r.warnings())
    assert not r.dump("done").has(type="AdwActionRow", text="Preparing Download…")


def test_remove_asks_then_removes(program, gcc_world, phone):
    go = gcc_world.tmp / "go"
    phone.model()
    phone.pkexec(wait_for=go)
    r = gcc_world.run(program, [click(type="AdwButtonRow", text="Remove Speech Model"),
                                wait_for(type="AdwAlertDialog"), dump("asked"),
                                click(type="GtkButton", text="Remove"), settle(), wait(600), dump("removing"),
                                {"op": "write_file", "path": str(go), "text": ""},
                                wait_for(type="AdwActionRow", text="Removing Speech Model…", gone=True,
                                         timeout=10000),
                                settle(200), dump("done")])
    alert = r.dump("asked").one(type="AdwAlertDialog")
    assert (alert["text"], alert["subtitle"]) == (
        "Remove Speech Model?",
        "This frees about 640 MB. Switching speech to text on again downloads the model again.")
    rem = r.dump("removing")
    assert rem.has(type="AdwActionRow", text="Removing Speech Model…")
    assert not rem.has(type="AdwButtonRow")
    done = r.dump("done")
    assert not done.has(type="AdwButtonRow") and not done.has(type="AdwAlertDialog")
    assert phone.pkexec_calls() == [[str(phone.path(PKG_TOOL)), "remove", "plinius-dictate-models"]]
    assert not phone.path(MODEL).exists()


def test_remove_cancelled_keeps_the_model(program, gcc_world, phone):
    phone.model()
    phone.pkexec()
    r = gcc_world.run(program, [click(type="AdwButtonRow", text="Remove Speech Model"),
                                wait_for(type="AdwAlertDialog"), click(type="GtkButton", text="Cancel"),
                                settle(200), dump("done")])
    assert phone.pkexec_calls() == []
    assert r.dump("done").has(type="AdwButtonRow", text="Remove Speech Model")


def test_remove_failing_says_so(program, gcc_world, phone):
    phone.model()
    phone.pkexec(code=9, install=False)
    r = gcc_world.run(program, [click(type="AdwButtonRow", text="Remove Speech Model"),
                                wait_for(type="AdwAlertDialog"), click(type="GtkButton", text="Remove"),
                                wait_for(type="AdwAlertDialog", text="Speech Model Not Removed"),
                                dump("done")])
    alert = r.dump("done").one(type="AdwAlertDialog")
    assert alert["subtitle"] == "Another software installation is running. Try again when it is done."
    assert r.dump("done").has(type="AdwButtonRow", text="Remove Speech Model")
    # Without output nothing follows the code
    assert [m["message"] for m in r.warnings()] == ["plinius-pkg ended with 9"]


def test_remove_without_pkexec(program, gcc_world, phone):
    phone.model()
    r = gcc_world.run(program, [click(type="AdwButtonRow", text="Remove Speech Model"),
                                wait_for(type="AdwAlertDialog"), click(type="GtkButton", text="Remove"),
                                wait_for(type="AdwAlertDialog", text="Speech Model Not Removed"), dump("done")])
    assert r.dump("done").one(type="AdwAlertDialog")["subtitle"] == "Check the internet connection and try again."


def test_while_nothing_is_downloaded_yet_the_row_says_preparing(program, gcc_world, phone):
    go = gcc_world.tmp / "go"
    phone.pacman("Download Size   : 1.00 MiB")
    phone.pkexec(wait_for=go)
    r = gcc_world.run(program, [set_active(True, type="AdwSwitchRow"), wait_for(type="AdwAlertDialog"),
                                click(type="GtkButton", text="Download"), wait(700), dump("waiting"),
                                {"op": "write_file", "path": str(go), "text": ""}, settle(300)])
    assert r.dump("waiting").has(type="AdwActionRow", text="Preparing Download…")
    assert not r.dump("waiting").has(type="AdwActionRow", text="Downloading Speech Model…")
