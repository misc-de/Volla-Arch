# The group "Access" on an app's page under Apps (cc-plinius-appaccess.c): plinius-appaccessd's
# policies and counts per app, the camera's answer shared with the portal's permission store. The
# group is the page's group of Required Permissions, the four rows on top of the page's rows.
# The daemon (org.plinius.AppAccess1, interface from its C source) and the permission store are
# fakes on the test's private buses.

import time

import pytest

from conftest import PKG
from gcc_harness import (ROW_TYPES, build_panel, call, dump, gcc_session, gcc_world, interface_xml,  # noqa: F401
                         select, settle)

AA = "org.plinius.AppAccess1"
AA_PATH = "/org/plinius/AppAccess1"
AA_XML = interface_xml(PKG / "plinius-appaccess" / "plinius-appaccessd.c", AA)
NL = "org.plinius.NetLog1"
NL_PATH = "/org/plinius/NetLog1"
NL_XML = interface_xml(PKG / "plinius-netlog" / "plinius-netlogd.c", NL)
PS = "org.freedesktop.impl.portal.PermissionStore"
PS_PATH = "/org/freedesktop/impl/portal/PermissionStore"
# The methods the group uses, as xdg-desktop-portal's interface file has them
PS_XML = f"""<node><interface name="{PS}">
 <method name="SetPermission"><arg name="table" type="s" direction="in"/>
  <arg name="create" type="b" direction="in"/><arg name="id" type="s" direction="in"/>
  <arg name="app" type="s" direction="in"/><arg name="permissions" type="as" direction="in"/></method>
 <method name="DeletePermission"><arg name="table" type="s" direction="in"/>
  <arg name="id" type="s" direction="in"/><arg name="app" type="s" direction="in"/></method>
 <method name="GetPermission"><arg name="table" type="s" direction="in"/>
  <arg name="id" type="s" direction="in"/><arg name="app" type="s" direction="in"/>
  <arg name="permissions" type="as" direction="out"/></method>
</interface></node>"""
NOT_FOUND = {"error": "org.freedesktop.portal.Error.NotFound", "message": "No entry for camera"}

GLUE = r'''
#include <gio/gdesktopappinfo.h>
#include "gcc_driver.h"
#include "cc-plinius-appaccess.h"

/* As the Apps panel has it: the permissions group of the app's page, GNOME's camera and microphone
 * switches in it for Flatpak apps; the group of Required Permissions without a title (Required Permissions for
 * Flatpak apps, Global Shortcuts when the app has some, the group hidden without both); another
 * group below */
static GtkWidget *permissions, *camera, *microphone, *required, *builtin, *shortcuts;

/* What the panel's usage section does before: the rows for the app, the group shown with them */
static void
upstream_rows (const char *rows)
{
    gtk_widget_set_visible (builtin, rows && strstr (rows, "builtin"));
    gtk_widget_set_visible (shortcuts, rows && strstr (rows, "shortcuts"));
    gtk_widget_set_visible (required, gtk_widget_get_visible (builtin) || gtk_widget_get_visible (shortcuts));
}

static void
update (GccTest *t, const char *app)
{
    g_autoptr(GDesktopAppInfo) info = g_desktop_app_info_new (app);

    g_assert (info != NULL);
    cc_plinius_appaccess_update_app_page (t->panel, G_APP_INFO (info), required, camera, microphone);
}

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *below = adw_preferences_group_new ();
    const char *rows = gcc_test_param (t, "rows");

    permissions = adw_preferences_group_new ();
    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (permissions), "Permissions");
    if (!g_strcmp0 (gcc_test_param (t, "camera"), "yes")) {
        camera = adw_switch_row_new ();
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (camera), "GNOME Camera");
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (permissions), camera);
        microphone = adw_switch_row_new ();
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (microphone), "GNOME Microphone");
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (permissions), microphone);
    }
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (permissions));
    required = adw_preferences_group_new ();
    builtin = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (builtin), "Required Permissions");
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (required), builtin);
    shortcuts = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (shortcuts), "Global Shortcuts");
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (required), shortcuts);
    upstream_rows (rows ? rows : "builtin");
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (required));
    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (below), "Usage");
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (below));
    /* An app info without an id (made from a command line) */
    if (gcc_test_param (t, "commandline")) {
        g_autoptr(GAppInfo) info = g_app_info_create_from_commandline (gcc_test_param (t, "commandline"), "Loose",
                                                                      G_APP_INFO_CREATE_NONE, NULL);

        g_assert (info != NULL && g_app_info_get_id (info) == NULL);
        cc_plinius_appaccess_update_app_page (t->panel, info, required, camera, microphone);
    }
    if (gcc_test_param (t, "app"))
        update (t, gcc_test_param (t, "app"));
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "update")) {
        /* Another app's page: the panel's rows for it first */
        upstream_rows (json_array_get_length (args) > 1 ? json_array_get_string_element (args, 1) : "builtin");
        update (t, json_array_get_string_element (args, 0));
        json_builder_add_null_value (result);
        return TRUE;
    }
    return FALSE;
}
'''

CHOICES = ["Ask", "Allow", "Don't Allow"]


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("appaccess"), "appaccess",
                       ["cc-plinius-appaccess.c"], GLUE)


def desktop(world, app_id, flatpak=None):
    text = f"[Desktop Entry]\nType=Application\nName={app_id.title()}\nExec=/bin/true\n"
    if flatpak:
        text += f"X-Flatpak={flatpak}\n"
    (world.data_dir / "applications" / f"{app_id}.desktop").write_text(text)


def daemon(world, resources=None, **kw):
    """The daemon's Get answers resources: [(name, policy, allowed, denied, last_allowed,
    last_denied)]."""
    entries = ", ".join(f"({n!r}, uint32 {p}, uint32 {a}, uint32 {d}, int64 {la}, int64 {ld})"
                        for n, p, a, d, la, ld in (resources or []))
    reply = f"([{entries}],)" if entries else "(@a(suuuxx) [],)"
    world.service("system", AA, AA_PATH, AA_XML, replies={"Get": kw.pop("get", reply), **kw})


def store(world, camera=None):
    """The portal's permission store; camera: the app's answer ("yes", "no"), None: no entry."""
    reply = f"([{camera!r}],)" if camera else NOT_FOUND
    world.service("session", PS, PS_PATH, PS_XML, replies={"GetPermission": reply})


def netlog(world, rows=None, **kw):
    """plinius-netlogd; rows: [(app, count)] of its Query answer."""
    entries = ", ".join(f"({app!r}, '/usr/bin/x', uint32 6, uint16 443, '192.0.2.1', 'example.org', "
                        f"uint32 2, uint32 {count}, int64 0, int64 0)" for app, count in (rows or []))
    reply = f"([{entries}],)" if entries else "(@a(ssuqssuuxx) [],)"
    world.service("system", NL, NL_PATH, NL_XML, replies={"Query": kw.pop("query", reply)})


def combo_rows(group):
    return [c for c in group.walk() if c.get("type") == "AdwComboRow"]


def access_rows(screen):
    group = screen.one(type="AdwPreferencesGroup", text="Access")
    return [(r["text"], r["selected_text"], r.get("subtitle")) for r in combo_rows(group)]


def group_titles(screen):
    return [g.get("text") for g in screen.find(type="AdwPreferencesGroup")]


def group_rows(screen, title):
    """The rows of the group with that title (None: no title), in order."""
    group = [g for g in screen.find(type="AdwPreferencesGroup") if g.get("text") == title]
    assert len(group) == 1, screen.outline()
    return [r.get("text") for r in group[0].walk() if r.get("type") in ROW_TYPES]


def today_epoch():
    """A time today (UTC, the program's zone), not in the last seconds before midnight."""
    now = time.time()
    if 86400 - now % 86400 < 5:
        time.sleep(6)
        now = time.time()
    return int(now)


def test_policies_and_counts_of_the_app(program, gcc_world):
    desktop(gcc_world, "org.example.Maps")
    today = today_epoch()
    old = 1767323040  # 2026-01-02 03:04 UTC
    daemon(gcc_world, [("camera", 1, 3, 0, today, 0),
                       ("microphone", 0, 2, 1, today, old),
                       ("security-key", 0, 0, 0, 0, 0),
                       ("network", 2, 7, 2, today, old)])
    store(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")], params={"app": "org.example.Maps.desktop"})
    screen = r.dump("page")
    hhmm = time.strftime("%H:%M", time.gmtime(today))
    assert access_rows(screen) == [
        ("Camera", "Allow", f"Used 3 times, last {hhmm}"),
        ("Microphone", "Ask", f"1 attempt blocked, last 01/02/26 03:04 · Used 2 times, last {hhmm}"),
        ("Security Key", "Ask", "Not used yet"),
        # Allowed network use is not counted here
        ("Network", "Don't Allow", "2 attempts blocked, last 01/02/26 03:04"),
    ]
    assert all(r["items"] == CHOICES
               for r in combo_rows(screen.one(type="AdwPreferencesGroup", text="Access")))
    # One group with what the app may use: our rows on top of the page's Required Permissions
    assert group_titles(screen) == ["Permissions", "Access", "Usage"]
    assert group_rows(screen, "Access") == ["Camera", "Microphone", "Security Key", "Network", "Required Permissions"]
    assert screen.one(type="AdwPreferencesGroup", text="Access")["subtitle"] == (
        "With “Ask”, the app is blocked when it tries, and a notification asks whether to allow it.")
    assert [c["args"] for c in gcc_world.calls(AA, "Get")] == [["org.example.Maps"]]
    assert [c["args"] for c in gcc_world.calls(PS, "GetPermission")] == [
        ["devices", "camera", "org.example.Maps"]]
    # Nothing was changed by showing it
    assert not gcc_world.calls(AA, "SetPolicy") and not gcc_world.calls(PS, "SetPermission")
    assert not r.warnings()


def test_singular_counts_and_used_and_blocked_together(program, gcc_world):
    desktop(gcc_world, "org.example.Cam")
    old = 1767323040
    daemon(gcc_world, [("camera", 0, 1, 1, old, old), ("network", 0, 0, 1, 0, old),
                       ("security-key", 1, 1, 0, old, 0)])
    store(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")], params={"app": "org.example.Cam.desktop"})
    assert access_rows(r.dump("page")) == [
        ("Camera", "Ask", "1 attempt blocked, last 01/02/26 03:04 · Used 1 time, last 01/02/26 03:04"),
        ("Microphone", "Ask", None),
        ("Security Key", "Allow", "Used 1 time, last 01/02/26 03:04"),
        ("Network", "Ask", "1 attempt blocked, last 01/02/26 03:04"),
    ]


def test_unknown_resources_and_policies_are_left_out(program, gcc_world):
    desktop(gcc_world, "org.example.App")
    daemon(gcc_world, [("speaker", 1, 5, 5, 1, 1), ("network", 7, 0, 3, 0, 1),
                       ("camera", 2, 0, 0, 0, 0)])
    store(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")], params={"app": "org.example.App.desktop"})
    assert access_rows(r.dump("page")) == [
        ("Camera", "Don't Allow", "Not used yet"),
        ("Microphone", "Ask", None),
        ("Security Key", "Ask", None),
        ("Network", "Ask", None),
    ]


def test_without_the_daemon_nothing_shows(program, gcc_world):
    desktop(gcc_world, "org.example.Flat", flatpak="org.example.Flat")
    store(gcc_world, camera="yes")
    r = gcc_world.run(program, [settle(), dump("page")],
                      params={"app": "org.example.Flat.desktop", "camera": "yes"})
    screen = r.dump("page")
    # The group as the page has it: no title, only its own rows
    assert group_titles(screen) == ["Permissions", None, "Usage"]
    assert group_rows(screen, None) == ["Required Permissions"]
    # GNOME's own camera and microphone switches stay the way to answer
    assert screen.has(type="AdwSwitchRow", text="GNOME Camera")
    assert screen.has(type="AdwSwitchRow", text="GNOME Microphone")
    assert not gcc_world.calls(PS)
    assert not r.warnings()


def test_flatpak_app_by_its_app_id_and_gnome_camera_and_microphone_hidden(program, gcc_world):
    desktop(gcc_world, "flat-launcher", flatpak="org.example.Flat")
    daemon(gcc_world, [("camera", 0, 0, 0, 0, 0)])
    store(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")],
                      params={"app": "flat-launcher.desktop", "camera": "yes"})
    screen = r.dump("page")
    assert [c["args"] for c in gcc_world.calls(AA, "Get")] == [["org.example.Flat"]]
    assert not screen.has(type="AdwSwitchRow", text="GNOME Camera")
    assert not screen.has(type="AdwSwitchRow", text="GNOME Microphone")
    assert "Access" in group_titles(screen)


@pytest.mark.parametrize("row, index, resource", [("Network", 1, "network"),
                                                   ("Security Key", 2, "security-key"),
                                                   ("Microphone", 1, "microphone"),
                                                   ("Network", 0, "network")])
def test_choice_sets_the_policy(program, gcc_world, row, index, resource):
    desktop(gcc_world, "org.example.App")
    daemon(gcc_world, [("camera", 0, 0, 0, 0, 0), ("security-key", 1, 0, 0, 0, 0),
                       ("network", 2, 0, 0, 0, 0)])
    store(gcc_world)
    gcc_world.run(program, [settle(), select(index, type="AdwComboRow", text=row), settle()],
                  params={"app": "org.example.App.desktop"})
    assert [c["args"] for c in gcc_world.calls(AA, "SetPolicy")] == [
        ["org.example.App", resource, index]]
    # Only the camera is shared with the portal
    assert not gcc_world.calls(PS, "SetPermission") and not gcc_world.calls(PS, "DeletePermission")


@pytest.mark.parametrize("index, portal", [(1, ("SetPermission", ["devices", True, "camera",
                                                                  "org.example.App", ["yes"]])),
                                           (2, ("SetPermission", ["devices", True, "camera",
                                                                  "org.example.App", ["no"]])),
                                           (0, ("DeletePermission", ["devices", "camera",
                                                                     "org.example.App"]))])
def test_camera_choice_goes_to_the_portal_too(program, gcc_world, index, portal):
    desktop(gcc_world, "org.example.App")
    start = 2 if index != 2 else 1
    daemon(gcc_world, [("camera", start, 0, 0, 0, 0)])
    store(gcc_world)
    gcc_world.run(program, [settle(), select(index, type="AdwComboRow", text="Camera"), settle()],
                  params={"app": "org.example.App.desktop"})
    method, args = portal
    assert [c["args"] for c in gcc_world.calls(PS, method)] == [args]
    assert [c["args"] for c in gcc_world.calls(AA, "SetPolicy")] == [
        ["org.example.App", "camera", index]]


@pytest.mark.parametrize("answer, shown", [("no", "Don't Allow"), ("yes", "Allow")])
def test_portal_answer_is_taken_over(program, gcc_world, answer, shown):
    desktop(gcc_world, "org.example.App")
    daemon(gcc_world, [("camera", 0, 0, 0, 0, 0)])
    store(gcc_world, camera=answer)
    r = gcc_world.run(program, [settle(), dump("page")], params={"app": "org.example.App.desktop"})
    assert access_rows(r.dump("page"))[0][1] == shown
    # plinius-appaccess gets the same answer for cameras on USB; nothing goes back to the portal
    assert [c["args"] for c in gcc_world.calls(AA, "SetPolicy")] == [
        ["org.example.App", "camera", 1 if answer == "yes" else 2]]
    assert not gcc_world.calls(PS, "SetPermission") and not gcc_world.calls(PS, "DeletePermission")


@pytest.mark.parametrize("answer", ["yes", "ask"])
def test_portal_answer_already_held_or_unknown_changes_nothing(program, gcc_world, answer):
    desktop(gcc_world, "org.example.App")
    daemon(gcc_world, [("camera", 1, 0, 0, 0, 0)])
    store(gcc_world, camera=answer)
    r = gcc_world.run(program, [settle(), dump("page")], params={"app": "org.example.App.desktop"})
    assert access_rows(r.dump("page"))[0][1] == "Allow"
    assert not gcc_world.calls(AA, "SetPolicy")


def test_portal_with_an_empty_answer_changes_nothing(program, gcc_world):
    desktop(gcc_world, "org.example.App")
    daemon(gcc_world, [("camera", 0, 0, 0, 0, 0)])
    gcc_world.service("session", PS, PS_PATH, PS_XML, replies={"GetPermission": "(@as [],)"})
    r = gcc_world.run(program, [settle(), dump("page")], params={"app": "org.example.App.desktop"})
    assert access_rows(r.dump("page"))[0][1] == "Ask"
    assert not gcc_world.calls(AA, "SetPolicy")


def test_refused_change_shows_again_what_holds(program, gcc_world):
    desktop(gcc_world, "org.example.App")
    daemon(gcc_world, [("network", 2, 0, 0, 0, 0)],
           SetPolicy={"error": "org.freedesktop.DBus.Error.AccessDenied", "message": "Not allowed"})
    store(gcc_world)
    r = gcc_world.run(program, [settle(), select(1, type="AdwComboRow", text="Network"), settle(),
                                dump("page")],
                      params={"app": "org.example.App.desktop"})
    assert access_rows(r.dump("page"))[3][1] == "Don't Allow"
    assert len(gcc_world.calls(AA, "Get")) == 2
    assert [m["message"] for m in r.warnings()] == [
        "plinius-appaccess: cannot change the policy: GDBus.Error:"
        "org.freedesktop.DBus.Error.AccessDenied: Not allowed"]


def test_changes_elsewhere_are_read_again(program, gcc_world):
    desktop(gcc_world, "org.example.App")
    daemon(gcc_world, get=["([('network', uint32 0, uint32 0, uint32 0, int64 0, int64 0)],)",
                           "([('network', uint32 2, uint32 0, uint32 1, int64 0, "
                           "int64 1767323040)],)"])
    store(gcc_world)
    ctl = gcc_world.control("system")
    r = gcc_world.run(program, [
        settle(),
        # Another app's change is not this page's
        ctl.emit(AA_PATH, AA, "Changed", "('org.example.Other',)"), settle(),
        dump("other"),
        ctl.emit(AA_PATH, AA, "AccessDenied", "('org.example.App', 'network', uint32 2, uint32 1000)"),
        settle(), dump("denied")], params={"app": "org.example.App.desktop"})
    assert access_rows(r.dump("other"))[3][1:] == ("Ask", "No blocked attempts")
    assert access_rows(r.dump("denied"))[3][1:] == (
        "Don't Allow", "1 attempt blocked, last 01/02/26 03:04")
    assert len(gcc_world.calls(AA, "Get")) == 2


def test_page_of_another_app_reuses_the_group(program, gcc_world):
    desktop(gcc_world, "org.example.One")
    desktop(gcc_world, "org.example.Two")
    daemon(gcc_world, [("network", 1, 0, 0, 0, 0)])
    store(gcc_world)
    r = gcc_world.run(program, [settle(), call("update", "org.example.Two.desktop"), settle(),
                                dump("page")],
                      params={"app": "org.example.One.desktop"})
    assert group_titles(r.dump("page")) == ["Permissions", "Access", "Usage"]
    assert [c["args"] for c in gcc_world.calls(AA, "Get")] == [["org.example.One"],
                                                                ["org.example.Two"]]


def test_time_out_of_range_shows_no_time(program, gcc_world):
    desktop(gcc_world, "org.example.App")
    daemon(gcc_world, [("network", 0, 0, 1, 0, 2**63 - 1)])
    store(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")], params={"app": "org.example.App.desktop"})
    assert access_rows(r.dump("page"))[3][2] == "1 attempt blocked, last "


def test_an_app_without_id_shows_nothing(program, gcc_world):
    daemon(gcc_world, [("network", 1, 0, 0, 0, 0)])
    store(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")], params={"commandline": "/bin/true"})
    assert "Access" not in group_titles(r.dump("page"))
    assert group_rows(r.dump("page"), None) == ["Required Permissions"]
    assert not gcc_world.calls(AA)


def test_without_session_bus_only_the_daemon_is_asked(program, gcc_world):
    desktop(gcc_world, "org.example.App")
    daemon(gcc_world, [("camera", 0, 0, 0, 0, 0)])
    store(gcc_world, camera="no")
    r = gcc_world.run(program, [settle(), select(1, type="AdwComboRow", text="Camera"), settle(),
                                dump("page")],
                      params={"app": "org.example.App.desktop"}, session_bus=False)
    assert access_rows(r.dump("page"))[0][1] == "Allow"
    assert not gcc_world.calls(PS)
    assert [c["args"] for c in gcc_world.calls(AA, "SetPolicy")] == [
        ["org.example.App", "camera", 1]]


def test_signal_without_arguments_is_ignored(program, gcc_world):
    desktop(gcc_world, "org.example.App")
    daemon(gcc_world, [("network", 0, 0, 0, 0, 0)])
    store(gcc_world)
    ctl = gcc_world.control("system")
    gcc_world.run(program, [settle(), ctl.emit_raw(AA_PATH, AA, "Changed", "()"), settle()],
                  params={"app": "org.example.App.desktop"})
    assert len(gcc_world.calls(AA, "Get")) == 1


def test_an_older_answer_after_a_newer_question_is_dropped(program, gcc_world):
    desktop(gcc_world, "org.example.App")
    row = "('network', uint32 {}, uint32 0, uint32 0, int64 0, int64 0)"
    daemon(gcc_world, get=[f"([{row.format(0)}],)",
                           {"delay_ms": 400, "reply": f"([{row.format(2)}],)"},
                           f"([{row.format(1)}],)"])
    store(gcc_world)
    ctl = gcc_world.control("system")
    changed = ctl.emit(AA_PATH, AA, "Changed", "('org.example.App',)")
    r = gcc_world.run(program, [settle(), changed, {"op": "wait", "ms": 50}, changed,
                                {"op": "wait", "ms": 700}, dump("page")],
                      params={"app": "org.example.App.desktop"})
    assert len(gcc_world.calls(AA, "Get")) == 3
    assert access_rows(r.dump("page"))[3][1] == "Allow"


def test_portal_answer_after_a_newer_question_is_dropped(program, gcc_world):
    desktop(gcc_world, "org.example.App")
    daemon(gcc_world, [("camera", 0, 0, 0, 0, 0)])
    gcc_world.service("session", PS, PS_PATH, PS_XML, replies={"GetPermission": [
        {"delay_ms": 400, "reply": "(['no'],)"}, NOT_FOUND]})
    ctl = gcc_world.control("system")
    r = gcc_world.run(program, [settle(), ctl.emit(AA_PATH, AA, "Changed", "('org.example.App',)"),
                                {"op": "wait", "ms": 700}, dump("page")],
                      params={"app": "org.example.App.desktop"})
    assert len(gcc_world.calls(PS, "GetPermission")) == 2
    assert access_rows(r.dump("page"))[0][1] == "Ask"
    assert not gcc_world.calls(AA, "SetPolicy")


def test_without_rows_of_the_page_the_group_shows_with_ours(program, gcc_world):
    # An app that is no Flatpak: GNOME hides the group, Access shows it again
    desktop(gcc_world, "org.example.Native")
    daemon(gcc_world, [("network", 1, 0, 0, 0, 0)])
    store(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")],
                      params={"app": "org.example.Native.desktop", "rows": "none"})
    assert group_titles(r.dump("page")) == ["Permissions", "Access", "Usage"]
    assert group_rows(r.dump("page"), "Access") == ["Camera", "Microphone", "Security Key", "Network"]


def test_without_the_daemon_and_without_rows_the_group_stays_hidden(program, gcc_world):
    desktop(gcc_world, "org.example.Native")
    r = gcc_world.run(program, [settle(), dump("page")],
                      params={"app": "org.example.Native.desktop", "rows": "none"})
    assert group_titles(r.dump("page")) == ["Permissions", "Usage"]
    assert not r.warnings()


def test_global_shortcuts_stay_below_required_permissions(program, gcc_world):
    desktop(gcc_world, "org.example.App")
    daemon(gcc_world, [("network", 1, 0, 0, 0, 0)])
    store(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")],
                      params={"app": "org.example.App.desktop", "rows": "builtin shortcuts"})
    assert group_rows(r.dump("page"), "Access") == [
        "Camera", "Microphone", "Security Key", "Network", "Required Permissions", "Global Shortcuts"]


def test_page_without_answer_after_one_with_gives_the_group_back(program, gcc_world):
    desktop(gcc_world, "org.example.One")
    desktop(gcc_world, "org.example.Two")
    daemon(gcc_world, get=["([('network', uint32 1, uint32 0, uint32 0, int64 0, int64 0)],)",
                           {"error": "org.freedesktop.DBus.Error.AccessDenied", "message": "No"}])
    store(gcc_world)
    r = gcc_world.run(program, [settle(), dump("one"),
                                call("update", "org.example.Two.desktop", "shortcuts"), settle(),
                                dump("two")],
                      params={"app": "org.example.One.desktop"})
    assert group_titles(r.dump("one")) == ["Permissions", "Access", "Usage"]
    assert group_titles(r.dump("two")) == ["Permissions", None, "Usage"]
    assert group_rows(r.dump("two"), None) == ["Global Shortcuts"]
    # Neither our title nor our description stays
    assert "subtitle" not in r.dump("two").find(type="AdwPreferencesGroup")[1]


def test_network_with_the_connections_of_the_last_day(program, gcc_world):
    desktop(gcc_world, "org.example.App")
    daemon(gcc_world, [("network", 1, 0, 2, 0, 1767323040)])
    store(gcc_world)
    # Only the app's own rows count, all of them
    netlog(gcc_world, [("org.example.App", 5), ("org.example.Other", 40), ("org.example.App", 2),
                       ("service:plinius-pkg", 9)])
    r = gcc_world.run(program, [settle(), dump("page")], params={"app": "org.example.App.desktop"})
    assert access_rows(r.dump("page"))[3] == (
        "Network", "Allow", "2 attempts blocked, last 01/02/26 03:04 · 7 connections in the last 24 hours")
    assert [c["args"] for c in gcc_world.calls(NL, "Query")] == [[86400]]
    assert not r.warnings()


@pytest.mark.parametrize("rows, subtitle", [([("org.example.App", 1)], "1 connection in the last 24 hours"),
                                            ([("org.example.Other", 3)], "No connections in the last 24 hours"),
                                            ([], "No connections in the last 24 hours")])
def test_network_connections_alone(program, gcc_world, rows, subtitle):
    desktop(gcc_world, "org.example.App")
    daemon(gcc_world, [("network", 0, 0, 0, 0, 0)])
    store(gcc_world)
    netlog(gcc_world, rows)
    r = gcc_world.run(program, [settle(), dump("page")], params={"app": "org.example.App.desktop"})
    assert access_rows(r.dump("page"))[3][2] == subtitle


def test_network_without_an_answer_of_netlog_shows_the_attempts(program, gcc_world):
    desktop(gcc_world, "org.example.App")
    daemon(gcc_world, [("network", 0, 0, 0, 0, 0)])
    store(gcc_world)
    netlog(gcc_world, query={"error": "org.freedesktop.DBus.Error.AccessDenied", "message": "No"})
    r = gcc_world.run(program, [settle(), dump("page")], params={"app": "org.example.App.desktop"})
    assert access_rows(r.dump("page"))[3][2] == "No blocked attempts"
    assert len(gcc_world.calls(NL, "Query")) == 1


def test_connections_of_another_app_do_not_stay(program, gcc_world):
    desktop(gcc_world, "org.example.One")
    desktop(gcc_world, "org.example.Two")
    daemon(gcc_world, get=["([('network', uint32 1, uint32 0, uint32 0, int64 0, int64 0)],)",
                           "([('network', uint32 1, uint32 0, uint32 0, int64 0, int64 0)],)"])
    store(gcc_world)
    netlog(gcc_world, query=["([('org.example.One', '/usr/bin/x', uint32 6, uint16 443, '192.0.2.1', "
                             "'example.org', uint32 2, uint32 4, int64 0, int64 0)],)",
                             {"error": "org.freedesktop.DBus.Error.Failed", "message": "gone"}])
    r = gcc_world.run(program, [settle(), dump("one"), call("update", "org.example.Two.desktop"), settle(),
                                dump("two")],
                      params={"app": "org.example.One.desktop"})
    assert access_rows(r.dump("one"))[3][2] == "4 connections in the last 24 hours"
    assert access_rows(r.dump("two"))[3][2] == "No blocked attempts"
