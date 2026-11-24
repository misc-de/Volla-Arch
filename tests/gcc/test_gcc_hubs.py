# The hubs (shell/cc-plinius-hubs.c) and the hub panels Connected Devices and Accounts
# (cc-plinius-hub-panels.c): which panel gets a row on which hub's page instead of one in the
# panel list, the parameters a hub gets for one of its panels, the titles of panels opened as a
# hub's pages, and the rows: texts and icons from the panel list's model, following each panel's
# visibility, the group going with its rows.
#
# The program compiles GNOME's own shell model (shell/cc-shell-model.c with panels/common/cc-util.c,
# -DGCC_REAL_SHELL_MODEL), filled from the test's desktop entries as the panel loader fills it.
# The loader itself is a stand-in in the glue: it knows the panels of the param "built_in", each
# a small panel of the glue's type GccChildPanel.

import pytest

from gcc_harness import (Node, build_panel, call, click, dump, gcc_session, gcc_world, settle,  # noqa: F401
                         wait_for)

GLUE = r'''
#include <gio/gdesktopappinfo.h>

#include "gcc_driver.h"
#include "shell/cc-application.h"
#include "shell/cc-panel-loader.h"
#include "shell/cc-plinius-hubs.h"
#include "shell/cc-shell-model.h"

GType cc_connected_devices_panel_get_type (void);
GType cc_accounts_panel_get_type (void);

/* A panel that opens from a hub: says what it got */
#define GCC_TYPE_CHILD_PANEL (gcc_child_panel_get_type ())
G_DECLARE_FINAL_TYPE (GccChildPanel, gcc_child_panel, GCC, CHILD_PANEL, CcPanel)

struct _GccChildPanel {
    CcPanel parent_instance;
};

G_DEFINE_FINAL_TYPE (GccChildPanel, gcc_child_panel, CC_TYPE_PANEL)

static void
gcc_child_panel_class_init (GccChildPanelClass *klass)
{
}

static void
gcc_child_panel_init (GccChildPanel *self)
{
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (self), gtk_label_new ("Child Panel"));
}

/* Another hub: a page with a preferences page, as Security, System and Keyboard have */
#define GCC_TYPE_OTHER_HUB (gcc_other_hub_get_type ())
G_DECLARE_FINAL_TYPE (GccOtherHub, gcc_other_hub, GCC, OTHER_HUB, CcPanel)

struct _GccOtherHub {
    CcPanel parent_instance;
    AdwPreferencesPage *page;
};

G_DEFINE_FINAL_TYPE (GccOtherHub, gcc_other_hub, CC_TYPE_PANEL)

static void
gcc_other_hub_class_init (GccOtherHubClass *klass)
{
}

static void
gcc_other_hub_init (GccOtherHub *self)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();

    self->page = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), GTK_WIDGET (self->page));
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (self), toolbar);
    adw_navigation_page_set_title (ADW_NAVIGATION_PAGE (self), "Other Hub");
}

/* Stand-in for GNOME's panel loader: the panels built in are those of "built_in" */
GType
cc_panel_loader_get_panel_type (const char *name)
{
    const char *built_in = getenv ("GCC_TEST_BUILT_IN");
    g_auto(GStrv) names = g_strsplit (built_in ? built_in : "", ",", -1);

    return g_strv_contains ((const char *const *) names, name) ? GCC_TYPE_CHILD_PANEL : G_TYPE_INVALID;
}

static CcShellModel *
model (void)
{
    return cc_application_get_model (CC_APPLICATION (g_application_get_default ()));
}

static CcPanelVisibility
visibility_of (const char *name)
{
    if (g_str_equal (name, "hidden"))
        return CC_PANEL_HIDDEN;
    if (g_str_equal (name, "search"))
        return CC_PANEL_VISIBLE_IN_SEARCH;
    return CC_PANEL_VISIBLE;
}

/* The model as the loader fills it: one desktop entry per group of the param "model" (Name,
 * Comment, Icon), then the visibility a panel's static init would set */
static void
fill_model (GccTest *t)
{
    g_autoptr(GKeyFile) spec = g_key_file_new ();
    g_auto(GStrv) groups = NULL;
    const char *text = gcc_test_param (t, "model");

    if (!text || !g_key_file_load_from_data (spec, text, -1, G_KEY_FILE_NONE, NULL))
        return;
    groups = g_key_file_get_groups (spec, NULL);
    for (char **g = groups; *g; g++) {
        g_autoptr(GKeyFile) entry = g_key_file_new ();
        g_autoptr(GDesktopAppInfo) info = NULL;
        g_autofree char *visibility = g_key_file_get_string (spec, *g, "Visibility", NULL);
        const char *keys[] = { "Name", "Comment", "Icon" };

        g_key_file_set_string (entry, "Desktop Entry", "Type", "Application");
        /* A program that is there: GIO refuses an entry whose program is not */
        g_key_file_set_string (entry, "Desktop Entry", "Exec", "sh");
        for (guint i = 0; i < G_N_ELEMENTS (keys); i++) {
            g_autofree char *value = g_key_file_get_string (spec, *g, keys[i], NULL);

            if (value)
                g_key_file_set_string (entry, "Desktop Entry", keys[i], value);
        }
        info = g_desktop_app_info_new_from_keyfile (entry);
        g_assert (info);
        cc_shell_model_add_item (model (), CC_CATEGORY_DEVICES, G_APP_INFO (info), *g);
        if (visibility)
            cc_shell_model_set_panel_visibility (model (), *g, visibility_of (visibility));
    }
}

CcPanel *
gcc_test_create_panel (GccTest *t)
{
    const char *hub = gcc_test_param (t, "hub");
    CcPanel *panel;

    fill_model (t);
    /* The title as the panel loader gives it */
    if (g_strcmp0 (hub, "connected-devices") == 0)
        return g_object_new (cc_connected_devices_panel_get_type (), "window", t->window, "title",
                             "Connected Devices", NULL);
    if (g_strcmp0 (hub, "accounts") == 0)
        return g_object_new (cc_accounts_panel_get_type (), "window", t->window, "title", "Accounts", NULL);
    panel = g_object_new (GCC_TYPE_OTHER_HUB, "window", t->window, NULL);
    t->page = GCC_OTHER_HUB (panel)->page;
    return panel;
}

void
gcc_test_setup (GccTest *t)
{
    const char *hub = gcc_test_param (t, "hub");

    if (GCC_IS_OTHER_HUB (t->panel) && hub)
        cc_plinius_hub_add_rows (t->panel, t->page, hub);
}

static void
add_string_or_null (JsonBuilder *result, const char *text)
{
    if (text)
        json_builder_add_string_value (result, text);
    else
        json_builder_add_null_value (result);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "hub_of")) {
        add_string_or_null (result, cc_plinius_hub_of (json_array_get_string_element (args, 0)));
        return TRUE;
    }
    if (g_str_equal (name, "title")) {
        g_autofree char *title = cc_plinius_hub_panel_title (json_array_get_string_element (args, 0));

        add_string_or_null (result, title);
        return TRUE;
    }
    if (g_str_equal (name, "parameters")) {
        g_autoptr(GVariant) in = NULL;
        g_autoptr(GVariant) out = NULL;
        g_autofree char *text = NULL;

        if (!json_array_get_null_element (args, 1))
            in = g_variant_ref_sink (g_variant_parse (G_VARIANT_TYPE ("av"), json_array_get_string_element (args, 1),
                                                      NULL, NULL, NULL));
        out = cc_plinius_hub_parameters (json_array_get_string_element (args, 0), in);
        text = g_variant_print (out, TRUE);
        json_builder_add_string_value (result, text);
        return TRUE;
    }
    if (g_str_equal (name, "visibility")) {
        cc_shell_model_set_panel_visibility (model (), json_array_get_string_element (args, 0),
                                             visibility_of (json_array_get_string_element (args, 1)));
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "open")) {
        /* What GNOME Settings' window does for navigation.push of a tag that is no page yet */
        const char *tag = json_array_get_string_element (args, 0);
        AdwNavigationPage *page = cc_panel_get_static_subpage (t->panel, tag);

        if (!page) {
            json_builder_add_null_value (result);
            return TRUE;
        }
        adw_navigation_page_set_tag (page, tag);
        adw_navigation_view_push (t->nav, page);
        json_builder_begin_object (result);
        json_builder_set_member_name (result, "type");
        json_builder_add_string_value (result, G_OBJECT_TYPE_NAME (page));
        json_builder_set_member_name (result, "title");
        add_string_or_null (result, adw_navigation_page_get_title (page));
        json_builder_end_object (result);
        return TRUE;
    }
    return FALSE;
}
'''

CHILDREN = {"connected-devices": ["usb", "streaming", "printers", "mouse", "wacom"],
            "accounts": ["users", "online-accounts"],
            "keyboard": ["assistant"],
            "system": ["updates"],
            "security": ["updates"]}
ALL = sorted({p for ps in CHILDREN.values() for p in ps})

MODEL = {
    "usb": ("USB", "Charging, data and network over the cable", "plinius-usb"),
    "streaming": ("Streaming", "Show the screen on a TV", "plinius-cast-symbolic"),
    "printers": ("Printers", "Add printers, view printer jobs", "printer"),
    "mouse": ("Mouse & Touchpad", "Pointer speed, <primary> button", "input-mouse"),
    "wacom": ("Wacom Tablet", "Stylus and tablet", "input-tablet"),
    "users": ("Users", "Your account", "system-users"),
    "online-accounts": ("Online Accounts", "Mail, calendars, contacts", "goa-panel"),
    "assistant": ("Assistant Button", "Short, double and long press", "input-keyboard"),
    "updates": ("Updates", "System and app updates", "software-update-available"),
}


def model_text(panels=MODEL, visibility=None):
    out = []
    for pid, (name, comment, icon) in panels.items():
        out.append(f"[{pid}]")
        if name is not None:
            out.append(f"Name={name}")
        if comment is not None:
            out.append(f"Comment={comment}")
        if icon is not None:
            out.append(f"Icon={icon}")
        if visibility and pid in visibility:
            out.append(f"Visibility={visibility[pid]}")
    return "\n".join(out) + "\n"


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("hubs"), "hubs",
                       ["cc-plinius-hubs.c", "cc-plinius-hub-panels.c"], GLUE,
                       upstream=["shell/cc-shell-model.c", "panels/common/cc-util.c"],
                       flags=["-DGCC_REAL_SHELL_MODEL"])


def run(world, program, steps, hub=None, panels=MODEL, visibility=None, built_in=ALL, **kw):
    world.extra_env["GCC_TEST_BUILT_IN"] = ",".join(built_in)
    params = {"model": model_text(panels, visibility)}
    if hub:
        params["hub"] = hub
    return world.run(program, steps, params=params, **kw)


def hub_rows(screen):
    return [(r["text"], r.get("subtitle")) for r in screen.find(type="CcListRow")]


# ----- The table -----

@pytest.mark.parametrize("panel, hub", [
    ("usb", "connected-devices"), ("streaming", "connected-devices"),
    ("printers", "connected-devices"), ("mouse", "connected-devices"),
    ("wacom", "connected-devices"), ("users", "accounts"), ("online-accounts", "accounts"),
    ("assistant", "keyboard"),
    # Its first hub is the one it opens on
    ("updates", "system"),
    # Panels with a row of their own in the list
    ("wifi", None), ("security", None), ("", None),
])
def test_hub_of(program, gcc_world, panel, hub):
    r = run(gcc_world, program, [call("hub_of", panel)])
    assert r.value("hub_of") == hub


@pytest.mark.parametrize("parameters, result", [
    (None, "[<'usb'>]"),
    ("@av []", "[<'usb'>]"),
    # What was meant for the panel follows the page to open
    ("[<'network'>, <uint32 3>]", "[<'usb'>, <'network'>, <uint32 3>]"),
    # Phosh's quick settings say "no parameters" with an empty string: left out
    ("[<''>]", "[<'usb'>]"),
    ("[<''>, <'network'>, <''>]", "[<'usb'>, <'network'>]"),
])
def test_parameters_for_the_hub(program, gcc_world, parameters, result):
    r = run(gcc_world, program, [call("parameters", "usb", parameters)])
    assert r.value("parameters") == result


def test_title_of_a_panel_from_the_model(program, gcc_world):
    r = run(gcc_world, program, [call("title", "mouse", label="mouse"),
                                 call("title", "nothing", label="nothing")])
    assert r.value("mouse") == "Mouse & Touchpad"
    assert r.value("nothing") is None


# ----- Rows on a hub's page -----

def test_connected_devices_has_its_panels_in_the_tables_order(program, gcc_world):
    r = run(gcc_world, program, [settle(), dump("main")], hub="connected-devices")
    s = r.dump("main")
    assert s.has(type="CcConnectedDevicesPanel")
    assert hub_rows(s) == [
        ("USB", "Charging, data and network over the cable"),
        ("Streaming", "Show the screen on a TV"),
        ("Printers", "Add printers, view printer jobs"),
        # The names are no markup: shown as written
        ("Mouse & Touchpad", "Pointer speed, <primary> button"),
        ("Wacom Tablet", "Stylus and tablet"),
    ]
    rows = s.find(type="CcListRow")
    assert [(n["action"], n["target"]) for n in rows] == [
        ("navigation.push", f"'{p}'") for p in CHILDREN["connected-devices"]]
    # The model's icons, symbolic
    icons = [icons_of(n) for n in rows]
    assert icons == [["plinius-usb-symbolic"], ["plinius-cast-symbolic"], ["printer-symbolic"],
                     ["input-mouse-symbolic"], ["input-tablet-symbolic"]]
    assert not r.warnings()


def icons_of(node):
    return [n["icon"] for n in Node(node).walk() if n.get("type") == "GtkImage" and n.get("icon")]


def test_accounts_has_users_and_online_accounts(program, gcc_world):
    r = run(gcc_world, program, [settle(), dump("main")], hub="accounts")
    s = r.dump("main")
    assert s.has(type="CcAccountsPanel")
    assert hub_rows(s) == [("Users", "Your account"), ("Online Accounts", "Mail, calendars, contacts")]


@pytest.mark.parametrize("hub, rows", [
    ("keyboard", [("Assistant Button", "Short, double and long press")]),
    ("system", [("Updates", "System and app updates")]),
    ("security", [("Updates", "System and app updates")]),
])
def test_rows_on_other_hubs(program, gcc_world, hub, rows):
    r = run(gcc_world, program, [settle(), dump("main")], hub=hub)
    assert hub_rows(r.dump("main")) == rows


def test_a_panel_not_built_in_or_without_desktop_file_gets_no_row(program, gcc_world):
    panels = {k: v for k, v in MODEL.items() if k != "printers"}
    r = run(gcc_world, program, [settle(), dump("main"), call("open", "printers", label="printers"),
                                 call("open", "wacom", label="wacom")],
            hub="connected-devices", panels=panels,
            built_in=[p for p in ALL if p != "wacom"])
    assert [t for t, _ in hub_rows(r.dump("main"))] == ["USB", "Streaming", "Mouse & Touchpad"]
    # Neither is a page of the hub
    assert r.value("printers") is None
    assert r.value("wacom") is None


def test_a_panel_without_description_or_themed_icon(program, gcc_world, tmp_path):
    icon = tmp_path / "usb.png"
    icon.write_bytes(b"")
    panels = {"usb": ("USB", None, str(icon)), "streaming": ("Streaming", "x", None)}
    r = run(gcc_world, program, [settle(), dump("main")], hub="connected-devices", panels=panels)
    s = r.dump("main")
    assert hub_rows(s) == [("USB", None), ("Streaming", "x")]
    # A file icon is no icon name: the row has none
    assert [icons_of(n) for n in s.find(type="CcListRow")] == [[], []]


def test_rows_follow_the_panels_visibility(program, gcc_world):
    r = run(gcc_world, program, [
        settle(), dump("start"),
        call("visibility", "mouse", "visible", label="v1"), settle(), dump("mouse back"),
        call("visibility", "usb", "search", label="v2"), call("visibility", "streaming", "hidden",
                                                              label="v3"),
        settle(), dump("fewer"),
    ], hub="connected-devices", visibility={"mouse": "hidden", "wacom": "search"})
    # Hidden, or only in the search: no row on the hub either
    assert [t for t, _ in hub_rows(r.dump("start"))] == ["USB", "Streaming", "Printers"]
    # A mouse plugged in
    assert [t for t, _ in hub_rows(r.dump("mouse back"))] == ["USB", "Streaming", "Printers",
                                                              "Mouse & Touchpad"]
    assert [t for t, _ in hub_rows(r.dump("fewer"))] == ["Printers", "Mouse & Touchpad"]


def test_the_group_goes_with_its_rows(program, gcc_world):
    r = run(gcc_world, program, [
        settle(), dump("start"), call("visibility", "users", "hidden", label="a"),
        call("visibility", "online-accounts", "hidden", label="b"), settle(), dump("none"),
        call("visibility", "online-accounts", "visible", label="c"), settle(), dump("one")],
        hub="accounts")
    assert r.dump("start").has(type="AdwPreferencesGroup")
    assert not r.dump("none").has(type="AdwPreferencesGroup")
    assert not r.dump("none").has(type="CcListRow")
    assert hub_rows(r.dump("one")) == [("Online Accounts", "Mail, calendars, contacts")]


def test_a_hub_without_panels_shows_no_group(program, gcc_world):
    r = run(gcc_world, program, [settle(), dump("main")], hub="wifi")
    assert not r.dump("main").has(type="AdwPreferencesGroup")


def test_a_hubs_panel_opens_as_its_page_with_its_title(program, gcc_world):
    r = run(gcc_world, program, [settle(), call("open", "mouse"),
                                 wait_for(type="GccChildPanel"), dump("opened")],
            hub="connected-devices")
    assert r.value("open") == {"type": "GccChildPanel", "title": "Mouse & Touchpad"}
    page = r.dump("opened").one(type="GccChildPanel")
    assert page["text"] == "Mouse & Touchpad"
    assert r.dump("opened").has(type="GtkLabel", text="Child Panel")


