# The Security panel (cc-security-panel.c): its page is Device Security
# (cc_plinius_security_setup_panel), with the hub's row to Updates below it
# (shell/cc-plinius-hubs.c, compiled along with GNOME's shell model), the Encryption page of
# plinius-crypt as a subpage when there is one, and Lock & Unlock as a page built when opened,
# when that panel is built in.
#
# The page's content and the Encryption page are stand-ins in the glue (they are tested in
# test_gcc_security.py and test_gcc_crypt.py): the stand-in page has the same shape (a toolbar
# view holding a preferences page), the Encryption page is there unless GCC_TEST_NO_CRYPT is set.
# The panel loader is a stand-in too: it knows "lock" and "updates" as listed in GCC_TEST_BUILT_IN.

import pytest

from gcc_harness import build_panel, call, dump, gcc_session, gcc_world, push, settle, wait_for  # noqa: F401

GLUE = r'''
#include <gio/gdesktopappinfo.h>

#include "gcc_driver.h"
#include "cc-plinius-crypt.h"
#include "cc-plinius-security.h"
#include "shell/cc-application.h"
#include "shell/cc-panel-loader.h"
#include "shell/cc-shell-model.h"

GType cc_security_panel_get_type (void);

/* ----- Stand-ins for the panel's parts ----- */

void
cc_plinius_security_setup_panel (CcPanel *panel)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *page = adw_preferences_page_new ();
    GtkWidget *group = adw_preferences_group_new ();
    GtkWidget *row = adw_action_row_new ();

    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (group), "Checks");
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), "Screen Lock");
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), row);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (group));
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), page);
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (panel), toolbar);
}

AdwNavigationPage *
cc_plinius_crypt_page_new (void)
{
    if (getenv ("GCC_TEST_NO_CRYPT"))
        return NULL;
    return adw_navigation_page_new_with_tag (gtk_label_new ("Encryption Content"), "Encryption", "encryption");
}

#define GCC_TYPE_STAND_IN_PANEL (gcc_stand_in_panel_get_type ())
G_DECLARE_FINAL_TYPE (GccStandInPanel, gcc_stand_in_panel, GCC, STAND_IN_PANEL, CcPanel)

struct _GccStandInPanel {
    CcPanel parent_instance;
};

G_DEFINE_FINAL_TYPE (GccStandInPanel, gcc_stand_in_panel, CC_TYPE_PANEL)

static void
gcc_stand_in_panel_class_init (GccStandInPanelClass *klass)
{
}

static void
gcc_stand_in_panel_init (GccStandInPanel *self)
{
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (self), gtk_label_new ("Stand-in Panel"));
}

GType
cc_panel_loader_get_panel_type (const char *name)
{
    const char *built_in = getenv ("GCC_TEST_BUILT_IN");
    g_auto(GStrv) names = g_strsplit (built_in ? built_in : "", ",", -1);

    return g_strv_contains ((const char *const *) names, name) ? GCC_TYPE_STAND_IN_PANEL : G_TYPE_INVALID;
}

/* The panel list's model, as the loader fills it */
static void
add_to_model (const char *id, const char *name, const char *comment)
{
    CcShellModel *model = cc_application_get_model (CC_APPLICATION (g_application_get_default ()));
    g_autoptr(GKeyFile) entry = g_key_file_new ();
    g_autoptr(GDesktopAppInfo) info = NULL;

    g_key_file_set_string (entry, "Desktop Entry", "Type", "Application");
    g_key_file_set_string (entry, "Desktop Entry", "Exec", "sh");
    g_key_file_set_string (entry, "Desktop Entry", "Name", name);
    g_key_file_set_string (entry, "Desktop Entry", "Comment", comment);
    g_key_file_set_string (entry, "Desktop Entry", "Icon", "emblem-system");
    info = g_desktop_app_info_new_from_keyfile (entry);
    cc_shell_model_add_item (model, CC_CATEGORY_PRIVACY, G_APP_INFO (info), id);
}

CcPanel *
gcc_test_create_panel (GccTest *t)
{
    add_to_model ("updates", "Updates", "System and app updates");
    add_to_model ("lock", "Lock & Unlock", "Password, pattern, fingerprint, face");
    /* The title as the panel loader gives it */
    return g_object_new (cc_security_panel_get_type (), "window", t->window, "title", "Security", NULL);
}

void
gcc_test_setup (GccTest *t)
{
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "subpages")) {
        GList *pages = cc_panel_get_subpages (t->panel);
        g_autoptr(GPtrArray) tags = g_ptr_array_new ();

        for (GList *l = pages; l; l = l->next)
            g_ptr_array_add (tags, (gpointer) adw_navigation_page_get_tag (l->data));
        g_list_free (pages);
        g_ptr_array_sort_values (tags, (GCompareFunc) g_strcmp0);
        json_builder_begin_array (result);
        for (guint i = 0; i < tags->len; i++)
            json_builder_add_string_value (result, tags->pdata[i]);
        json_builder_end_array (result);
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
        json_builder_add_string_value (result, adw_navigation_page_get_title (page));
        return TRUE;
    }
    return FALSE;
}
'''


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("security-panel"), "security-panel",
                       ["cc-security-panel.c", "cc-plinius-hubs.c"], GLUE,
                       upstream=["shell/cc-shell-model.c", "panels/common/cc-util.c"],
                       flags=["-DGCC_REAL_SHELL_MODEL"])


def run(world, program, steps, built_in=("lock", "updates"), crypt=True, **kw):
    world.extra_env["GCC_TEST_BUILT_IN"] = ",".join(built_in)
    if not crypt:
        world.extra_env["GCC_TEST_NO_CRYPT"] = "1"
    return world.run(program, steps, **kw)


def test_device_security_with_the_row_to_updates(program, gcc_world):
    r = run(gcc_world, program, [settle(), dump("main"), call("subpages")])
    s = r.dump("main")
    assert s.one(type="CcSecurityPanel")["text"] == "Security"
    # The checks first, the hub's row below them
    groups = s.find(type="AdwPreferencesGroup")
    assert [g.get("text") for g in groups] == ["Checks", None]
    row = s.one(type="CcListRow")
    assert (row["text"], row["subtitle"]) == ("Updates", "System and app updates")
    assert (row["action"], row["target"]) == ("navigation.push", "'updates'")
    # The other pages are built when opened
    assert r.value("subpages") == ["encryption"]
    assert not r.warnings()


def test_the_encryption_page_is_a_page_of_this_panel(program, gcc_world):
    r = run(gcc_world, program, [settle(), push("encryption"),
                                 wait_for(type="AdwNavigationPage", tag="encryption"), dump("page")])
    assert r.dump("page").one(type="AdwNavigationPage", tag="encryption")["text"] == "Encryption"
    assert r.dump("page").has(type="GtkLabel", text="Encryption Content")


def test_without_plinius_crypt_no_encryption_page(program, gcc_world):
    r = run(gcc_world, program, [settle(), call("subpages"), dump("main")], crypt=False)
    assert r.value("subpages") == []
    # The page itself is there as before
    assert r.dump("main").has(type="AdwPreferencesGroup", text="Checks")


def test_lock_and_unlock_opens_as_a_page_of_this_panel(program, gcc_world):
    r = run(gcc_world, program, [settle(), call("open", "lock"), wait_for(type="GccStandInPanel"),
                                 dump("lock")])
    # Built when opened, titled from the panel list's model: back leads here, not to the list
    assert r.value("open") == "Lock & Unlock"
    assert r.dump("lock").one(type="GccStandInPanel")["text"] == "Lock & Unlock"


def test_the_updates_row_opens_updates_here(program, gcc_world):
    r = run(gcc_world, program, [settle(), call("open", "updates"), wait_for(type="GccStandInPanel"),
                                 dump("updates")])
    assert r.value("open") == "Updates"
    assert r.dump("updates").one(type="GccStandInPanel")["text"] == "Updates"


def test_without_the_lock_panel_no_page_for_it(program, gcc_world):
    r = run(gcc_world, program, [settle(), call("open", "lock")], built_in=("updates",))
    assert r.value("open") is None


def test_without_updates_no_row_to_it(program, gcc_world):
    r = run(gcc_world, program, [settle(), dump("main")], built_in=("lock",))
    s = r.dump("main")
    assert not s.has(type="CcListRow")
    assert [g.get("text") for g in s.find(type="AdwPreferencesGroup")] == ["Checks"]
