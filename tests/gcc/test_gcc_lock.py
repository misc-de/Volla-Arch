# Lock & Unlock (cc-lock-panel.c): one page with the user page's ways to unlock (only those, section
# UNLOCK) and the Screen Lock page's groups moved over, the user being one's own account as
# accountsservice lists it, with the user-accounts permission of polkit.
#
# On the "system" bus: a fake accountsservice (org.freedesktop.Accounts with users, interfaces
# from the host's /usr/share/dbus-1/interfaces) and a fake polkit authority. libaccountsservice's
# ActUserManager and polkit's PolkitPermission are the real libraries. The user page and the
# Screen Lock page are stand-ins in the glue (GNOME's users and privacy panels, not this file):
# the user page shows whom it was given and with which permission.

import os

import pytest

from gcc_harness import (build_panel, call, compile_resources, dump, gcc_session,  # noqa: F401
                         gcc_world, settle, wait, wait_for)

GLUE = r'''
#include <act/act.h>
#include <polkit/polkit.h>

#include "gcc_driver.h"
#include "cc-lock-panel.h"
#include "cc-user-page.h"
#include "../../privacy/screen/cc-screen-page.h"

/* ----- Stand-in of the user page: says whom it shows ----- */

struct _CcUserPage {
    AdwNavigationPage parent_instance;
    AdwPreferencesPage *page;
    GtkLabel *who;
    ActUser *user;
    int section;
    int set_user_calls;
};

G_DEFINE_FINAL_TYPE (CcUserPage, cc_user_page, ADW_TYPE_NAVIGATION_PAGE)

static void
cc_user_page_dispose (GObject *object)
{
    g_clear_object (&CC_USER_PAGE (object)->user);
    G_OBJECT_CLASS (cc_user_page_parent_class)->dispose (object);
}

static void
cc_user_page_class_init (CcUserPageClass *klass)
{
    G_OBJECT_CLASS (klass)->dispose = cc_user_page_dispose;
}

static void
cc_user_page_init (CcUserPage *self)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *group = adw_preferences_group_new ();

    self->section = -1;
    self->page = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    self->who = GTK_LABEL (gtk_label_new ("No User"));
    gtk_widget_set_name (GTK_WIDGET (self->who), "who");
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (self->who));
    adw_preferences_page_add (self->page, ADW_PREFERENCES_GROUP (group));
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), GTK_WIDGET (self->page));
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (self), toolbar);
}

CcUserPage *
cc_user_page_new (void)
{
    return g_object_new (CC_TYPE_USER_PAGE, NULL);
}

void
cc_user_page_set_section (CcUserPage *self, CcUserPageSection section)
{
    self->section = section;
}

AdwPreferencesPage *
cc_user_page_get_preferences_page (CcUserPage *self)
{
    return self->page;
}

void
cc_user_page_set_user (CcUserPage *self, ActUser *user, GPermission *permission)
{
    g_autofree char *text = g_strdup_printf ("%s (%s, uid %u)%s", act_user_get_real_name (user),
                                             act_user_get_user_name (user), (guint) act_user_get_uid (user),
                                             permission && g_permission_get_allowed (permission) ? " allowed" : "");

    g_set_object (&self->user, user);
    self->set_user_calls++;
    gtk_label_set_text (self->who, text);
}

/* ----- Stand-in of the Screen Lock page: one group that moves ----- */

struct _CcScreenPage {
    AdwNavigationPage parent_instance;
    AdwPreferencesGroup *group;
};

G_DEFINE_FINAL_TYPE (CcScreenPage, cc_screen_page, ADW_TYPE_NAVIGATION_PAGE)

static int screen_pages_alive;

static void
cc_screen_page_finalize (GObject *object)
{
    screen_pages_alive--;
    G_OBJECT_CLASS (cc_screen_page_parent_class)->finalize (object);
}

static void
cc_screen_page_class_init (CcScreenPageClass *klass)
{
    G_OBJECT_CLASS (klass)->finalize = cc_screen_page_finalize;
}

static void
cc_screen_page_init (CcScreenPage *self)
{
    GtkWidget *row = adw_switch_row_new ();

    screen_pages_alive++;
    self->group = ADW_PREFERENCES_GROUP (g_object_ref_sink (adw_preferences_group_new ()));
    adw_preferences_group_set_title (self->group, "Screen Lock");
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), "Automatic Screen Lock");
    adw_preferences_group_add (self->group, row);
}

void
cc_screen_page_move_groups (CcScreenPage *self, AdwPreferencesPage *to)
{
    adw_preferences_page_add (to, self->group);
    g_clear_object (&self->group);
}

/* ----- The program ----- */

static CcPanel *panel;

CcPanel *
gcc_test_create_panel (GccTest *t)
{
    if (gcc_test_param (t, "preload")) {
        /* The user manager is loaded already (another panel used it first) */
        ActUserManager *manager = act_user_manager_get_default ();
        gboolean loaded = FALSE;
        gint64 end = g_get_monotonic_time () + 10 * G_USEC_PER_SEC;

        while (!loaded && g_get_monotonic_time () < end) {
            g_main_context_iteration (NULL, FALSE);
            g_object_get (manager, "is-loaded", &loaded, NULL);
        }
        g_assert (loaded);
    }
    /* The title as the panel loader gives it */
    panel = g_object_new (CC_TYPE_LOCK_PANEL, "window", t->window, "title", "Lock & Unlock", NULL);
    return panel;
}

void
gcc_test_setup (GccTest *t)
{
}

static CcUserPage *
user_page (void)
{
    return CC_USER_PAGE (adw_navigation_page_get_child (ADW_NAVIGATION_PAGE (panel)));
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "page")) {
        CcUserPage *page = user_page ();

        json_builder_begin_object (result);
        json_builder_set_member_name (result, "section");
        json_builder_add_int_value (result, page->section);
        json_builder_set_member_name (result, "set_user_calls");
        json_builder_add_int_value (result, page->set_user_calls);
        json_builder_set_member_name (result, "uid");
        json_builder_add_int_value (result, page->user ? (gint64) act_user_get_uid (page->user) : -1);
        json_builder_set_member_name (result, "screen_pages_alive");
        json_builder_add_int_value (result, screen_pages_alive);
        json_builder_end_object (result);
        return TRUE;
    }
    if (g_str_equal (name, "unref_panel")) {
        /* The window lets go of the panel: the Screen Lock page goes with it */
        CcPanel *held = g_object_ref (t->panel);

        json_builder_begin_array (result);
        adw_navigation_view_replace (t->nav, NULL, 0);
        t->panel = panel = NULL;
        json_builder_add_int_value (result, screen_pages_alive);
        g_object_unref (held);
        json_builder_add_int_value (result, screen_pages_alive);
        json_builder_end_array (result);
        return TRUE;
    }
    return FALSE;
}
'''

ACCOUNTS = "org.freedesktop.Accounts"
ACCOUNTS_PATH = "/org/freedesktop/Accounts"
USER_IFACE = "org.freedesktop.Accounts.User"
INTERFACES = "/usr/share/dbus-1/interfaces"
POLKIT = "org.freedesktop.PolicyKit1"
POLKIT_PATH = "/org/freedesktop/PolicyKit1/Authority"
POLKIT_XML = """<node><interface name="org.freedesktop.PolicyKit1.Authority">
 <method name="CheckAuthorization">
  <arg type="(sa{sv})" name="subject" direction="in"/><arg type="s" name="action_id" direction="in"/>
  <arg type="a{ss}" name="details" direction="in"/><arg type="u" name="flags" direction="in"/>
  <arg type="s" name="cancellation_id" direction="in"/>
  <arg type="(bba{ss})" name="result" direction="out"/></method>
 <method name="CancelCheckAuthorization"><arg type="s" name="cancellation_id" direction="in"/></method>
 <signal name="Changed"/>
 <property name="BackendName" type="s" access="read"/>
 <property name="BackendVersion" type="s" access="read"/>
 <property name="BackendFeatures" type="u" access="read"/>
</interface></node>"""
PERMISSION = "org.gnome.controlcenter.user-accounts.administration"

RESOURCE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gresources>
  <gresource prefix="/org/gnome/control-center/privacy-test"><file>placeholder.txt</file></gresource>
</gresources>
"""


def _xml(name):
    path = f"{INTERFACES}/{name}.xml"
    if not os.path.exists(path):
        pytest.skip(f"no {path} (accountsservice) on the host")
    with open(path) as f:
        return f.read()


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    d = tmp_path_factory.mktemp("lock")
    # The privacy panel's resources the Screen Lock page's template is in: here a bundle of one
    # file, registered by the panel as it registers the real one
    res = d / "res"
    res.mkdir()
    (res / "placeholder.txt").write_text("x")
    (res / "privacy.gresource.xml").write_text(RESOURCE_XML)
    resource_c = compile_resources(d, res / "privacy.gresource.xml", "cc_privacy")
    return build_panel(gcc_session, d, "lock", ["cc-lock-panel.c"], GLUE,
                       pkgs=("accountsservice", "polkit-gobject-1"), sources=[resource_c])


def me():
    return os.getuid()


def accounts(world, users, own=True):
    """accountsservice with users [(uid, user name, real name)], in this order."""
    paths = [f"{ACCOUNTS_PATH}/User{uid}" for uid, _n, _r in users]
    world.service("system", ACCOUNTS, ACCOUNTS_PATH, _xml("org.freedesktop.Accounts"),
                  properties={"DaemonVersion": "'23.13.9'", "HasMultipleUsers": "true"},
                  replies={"ListCachedUsers": f"(@ao {paths},)"}, own=own)
    for (uid, name, real), path in zip(users, paths):
        world.service("system", ACCOUNTS, path, _xml("org.freedesktop.Accounts.User"),
                      properties={"Uid": f"uint64 {uid}", "UserName": f"'{name}'", "RealName": f"'{real}'",
                                  "HomeDirectory": f"'/home/{name}'", "Shell": "'/bin/bash'",
                                  "LocalAccount": "true", "Saved": "true"})


def polkit(world, allowed=True, own=True):
    world.service("system", POLKIT, POLKIT_PATH, POLKIT_XML,
                  properties={"BackendName": "'js'", "BackendVersion": "'127'", "BackendFeatures": "uint32 1"},
                  replies={"CheckAuthorization": f"(({'true' if allowed else 'false'}, false, @a{{ss}} {{}}),)"},
                  own=own)


USERS = [(me() + 1, "guest", "Guest User"), (me(), "owner", "Phone Owner"), (0, "root", "root")]


def who(screen):
    return screen.one(type="GtkLabel", name="who")["text"]


def test_ones_own_account_with_the_permission(program, gcc_world):
    accounts(gcc_world, USERS)
    polkit(gcc_world)
    r = gcc_world.run(program, [wait_for(type="GtkLabel", contains="Phone Owner"), call("page"), dump("main")])
    s = r.dump("main")
    assert who(s) == f"Phone Owner (owner, uid {me()}) allowed"
    page = r.value("page")
    # The user page shows the ways to unlock and nothing else (CC_USER_PAGE_SECTION_UNLOCK)
    assert page["section"] == 2
    assert page["uid"] == me()
    assert page["set_user_calls"] == 1
    # Its title, and the Screen Lock page's groups below its rows
    assert s.one(type="CcUserPage")["text"] == "Lock & Unlock"
    groups = [g.get("text") for g in s.find(type="AdwPreferencesGroup")]
    assert groups == [None, "Screen Lock"]
    assert s.has(type="AdwSwitchRow", text="Automatic Screen Lock")
    # Asked for the permission Your Account asks for
    checks = gcc_world.calls(POLKIT, "CheckAuthorization")
    assert checks and {c["args"][1] for c in checks} == {PERMISSION}
    assert not r.warnings()


def test_the_permission_as_polkit_says(program, gcc_world):
    accounts(gcc_world, USERS)
    polkit(gcc_world, allowed=False)
    r = gcc_world.run(program, [wait_for(type="GtkLabel", contains="Phone Owner"), dump("main")])
    assert who(r.dump("main")) == f"Phone Owner (owner, uid {me()})"


def test_a_user_manager_loaded_before_the_panel(program, gcc_world):
    accounts(gcc_world, USERS)
    polkit(gcc_world)
    r = gcc_world.run(program, [wait_for(type="GtkLabel", contains="Phone Owner"), call("page")],
                      params={"preload": "1"})
    assert r.value("page")["uid"] == me()
    assert r.value("page")["set_user_calls"] == 1


def test_without_ones_own_account_no_user(program, gcc_world):
    accounts(gcc_world, [u for u in USERS if u[0] != me()])
    polkit(gcc_world)
    r = gcc_world.run(program, [settle(), wait(500), settle(), call("page"), dump("main")])
    assert who(r.dump("main")) == "No User"
    assert r.value("page")["set_user_calls"] == 0
    # The groups are there all the same
    assert r.dump("main").has(type="AdwPreferencesGroup", text="Screen Lock")


def test_changes_to_ones_own_account_reach_the_page(program, gcc_world):
    accounts(gcc_world, USERS)
    polkit(gcc_world)
    c = gcc_world.control()
    r = gcc_world.run(program, [
        wait_for(type="GtkLabel", contains="Phone Owner"),
        c.set_property(f"{ACCOUNTS_PATH}/User{me()}", USER_IFACE, "RealName", "'New Name'"),
        c.emit(f"{ACCOUNTS_PATH}/User{me()}", USER_IFACE, "Changed", "()"),
        wait_for(type="GtkLabel", contains="New Name"), call("page", label="after own"),
        # Another user's change is not ours
        c.set_property(f"{ACCOUNTS_PATH}/User{me() + 1}", USER_IFACE, "RealName", "'Other'"),
        c.emit(f"{ACCOUNTS_PATH}/User{me() + 1}", USER_IFACE, "Changed", "()"),
        settle(), wait(300), settle(), call("page", label="after other"), dump("main")])
    own = r.value("after own")
    assert own["uid"] == me()
    assert own["set_user_calls"] >= 2
    other = r.value("after other")
    assert other["set_user_calls"] == own["set_user_calls"]
    assert who(r.dump("main")) == f"New Name (owner, uid {me()}) allowed"


def test_without_polkit_it_warns_and_shows_no_user(program, gcc_world):
    accounts(gcc_world, USERS)
    polkit(gcc_world, own=False)
    r = gcc_world.run(program, [settle(), wait(300), settle(), call("page"), dump("main")])
    assert who(r.dump("main")) == "No User"
    assert r.value("page")["set_user_calls"] == 0
    # The user manager is not even asked
    assert gcc_world.calls(ACCOUNTS, "ListCachedUsers") == []
    msgs = [m["message"] for m in r.warnings()]
    assert len(msgs) == 1 and msgs[0].startswith(f"Cannot create '{PERMISSION}' permission: ")
    # The page is whole otherwise
    assert r.dump("main").has(type="AdwPreferencesGroup", text="Screen Lock")


def test_the_screen_lock_page_lives_as_long_as_the_panel(program, gcc_world):
    accounts(gcc_world, USERS)
    polkit(gcc_world)
    r = gcc_world.run(program, [wait_for(type="GtkLabel", contains="Phone Owner"), call("page"),
                                call("unref_panel")])
    # Kept behind the moved groups (their settings bindings belong to it), freed with the panel
    assert r.value("page")["screen_pages_alive"] == 1
    # Still there while anything holds the panel, gone with it
    assert r.value("unref_panel") == [1, 0]
