# The entry rows of the list style IPv4 and IPv6 pages (ce-plinius-ip-row.c): which texts an
# address row and a netmask row take (empty is valid), the error mark while typing, the prefix of
# a netmask, the spoken hint when the user enters or leaves a wrong row, and group titles made
# from labels with a mnemonic.

import pytest

from gcc_harness import (build_panel, call, dump, gcc_session, gcc_world, set_text,  # noqa: F401
                         settle)

GLUE = r'''
#include <sys/socket.h>
#include "gcc_driver.h"
#include "ce-plinius-ip-row.h"

static GtkWidget *rows[4];
static const char *const names[] = { "v4", "v6", "mask", "number" };
static GPtrArray *announced;

/* Stands in for GTK's own: what the rows tell a screen reader (none runs in the test) */
void
gtk_accessible_announce (GtkAccessible *self, const char *message, GtkAccessibleAnnouncementPriority priority)
{
    g_ptr_array_add (announced, g_strdup_printf ("%s|%s|%d", adw_preferences_row_get_title (ADW_PREFERENCES_ROW (self)),
                                                 message, priority));
}

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *group = adw_preferences_group_new ();

    announced = g_ptr_array_new_with_free_func (g_free);
    rows[0] = ce_plinius_ip_row_new_address (AF_INET, "Address");
    rows[1] = ce_plinius_ip_row_new_address (AF_INET6, "Address 6");
    rows[2] = ce_plinius_ip_row_new_netmask ("Netmask");
    rows[3] = ce_plinius_ip_row_new_number ("Metric");
    for (guint i = 0; i < G_N_ELEMENTS (rows); i++)
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), rows[i]);
    gcc_test_add (t, group);
}

static GtkWidget *
row_named (const char *name)
{
    for (guint i = 0; i < G_N_ELEMENTS (names); i++)
        if (g_str_equal (names[i], name))
            return rows[i];
    g_assert_not_reached ();
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "state")) {
        /* [empty, valid, prefix] of each row */
        json_builder_begin_object (result);
        for (guint i = 0; i < G_N_ELEMENTS (rows); i++) {
            json_builder_set_member_name (result, names[i]);
            json_builder_begin_array (result);
            json_builder_add_boolean_value (result, ce_plinius_ip_row_is_empty (rows[i]));
            json_builder_add_boolean_value (result, ce_plinius_ip_row_is_valid (rows[i]));
            json_builder_add_int_value (result, ce_plinius_ip_row_get_prefix (rows[i]));
            json_builder_end_array (result);
        }
        json_builder_end_object (result);
        return TRUE;
    }
    if (g_str_equal (name, "focus")) {
        gtk_widget_grab_focus (row_named (json_array_get_string_element (args, 0)));
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "announced")) {
        json_builder_begin_array (result);
        for (guint i = 0; i < announced->len; i++)
            json_builder_add_string_value (result, announced->pdata[i]);
        json_builder_end_array (result);
        return TRUE;
    }
    if (g_str_equal (name, "title")) {
        g_autofree char *title = ce_plinius_group_title_from_mnemonic (json_array_get_string_element (args, 0));

        json_builder_add_string_value (result, title);
        return TRUE;
    }
    if (g_str_equal (name, "bad_family")) {
        /* Neither IPv4 nor IPv6: no row */
        json_builder_add_boolean_value (result, ce_plinius_ip_row_new_address (AF_UNIX, "Unix") == NULL);
        return TRUE;
    }
    if (g_str_equal (name, "not_a_row")) {
        g_autoptr(GtkWidget) label = g_object_ref_sink (gtk_label_new ("x"));

        json_builder_begin_array (result);
        json_builder_add_boolean_value (result, ce_plinius_ip_row_is_empty (label));
        json_builder_add_boolean_value (result, ce_plinius_ip_row_is_valid (label));
        json_builder_add_int_value (result, ce_plinius_ip_row_get_prefix (label));
        json_builder_end_array (result);
        return TRUE;
    }
    return FALSE;
}
'''

ROWS = {"v4": "Address", "v6": "Address 6", "mask": "Netmask", "number": "Metric"}


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("ip-row"), "ip_row",
                       ["ce-plinius-ip-row.c"], GLUE, pkgs=("libnm",),
                       upstream=("panels/network/ui-helpers.c",))


def fill(texts):
    return [set_text(texts[k], type="AdwEntryRow", text=ROWS[k]) for k in texts]


def marked(screen):
    """The rows marked as wrong."""
    return sorted(r["text"] for r in screen.find(type="AdwEntryRow") if "error" in r.get("classes", ()))


def test_empty_rows_are_valid_and_unmarked(program, gcc_world):
    r = gcc_world.run(program, [call("state"), dump("page")])
    assert r.value("state") == {"v4": [True, True, 0], "v6": [True, True, 0],
                                "mask": [True, True, 0], "number": [True, True, 0]}
    screen = r.dump("page")
    assert [row["text"] for row in screen.find(type="AdwEntryRow")] == list(ROWS.values())
    assert marked(screen) == []
    assert not r.warnings()


@pytest.mark.parametrize("v4, v6, ok", [
    ("192.168.1.10", "fe80::1", True),
    ("10.0.0.1", "2001:db8::42", True),
    ("192.168.1.300", "fe80::g", False),
    ("192.168.1", "1:2:3", False),
    ("fe80::1", "192.168.1.1", False),
])
def test_addresses_of_their_family(program, gcc_world, v4, v6, ok):
    r = gcc_world.run(program, [*fill({"v4": v4, "v6": v6}), call("state"), dump("page")])
    state = r.value("state")
    assert state["v4"][:2] == [False, ok] and state["v6"][:2] == [False, ok]
    assert marked(r.dump("page")) == ([] if ok else ["Address", "Address 6"])


@pytest.mark.parametrize("text, valid, prefix", [
    ("24", True, 24), ("0", True, 0), ("32", True, 32),
    ("33", False, 0), ("-1", False, 0),
    ("255.255.255.0", True, 24), ("255.255.0.0", True, 16), ("255.255.255.255", True, 32),
    ("255.255.256.0", False, 0), ("1.2.3", False, 0),
])
def test_netmask_as_prefix_or_dotted_mask(program, gcc_world, text, valid, prefix):
    r = gcc_world.run(program, [*fill({"mask": text}), call("state"), dump("page")])
    assert r.value("state")["mask"] == [False, valid, prefix]
    assert marked(r.dump("page")) == ([] if valid else ["Netmask"])


# Only digits make a prefix (strtol () alone would take "abc" as 0 and "24x" as 24)
@pytest.mark.parametrize("text", ["abc", "24x", " 24", "+24", "2 4", "99999999999999999999"])
def test_netmask_with_letters_is_not_valid(program, gcc_world, text):
    r = gcc_world.run(program, [*fill({"mask": text}), call("state"), dump("page")])
    assert r.value("state")["mask"] == [False, False, 0]
    assert marked(r.dump("page")) == ["Netmask"]


def test_number_row_is_not_checked(program, gcc_world):
    r = gcc_world.run(program, [*fill({"number": "not a number"}), call("state"), dump("page")])
    assert r.value("state")["number"] == [False, True, 0]
    assert marked(r.dump("page")) == []


def test_mark_goes_when_the_text_is_corrected(program, gcc_world):
    r = gcc_world.run(program, [*fill({"v4": "10.0.0"}), dump("wrong"),
                                *fill({"v4": "10.0.0.1"}), dump("right"),
                                *fill({"v4": ""}), dump("empty")])
    assert marked(r.dump("wrong")) == ["Address"]
    assert marked(r.dump("right")) == []
    assert marked(r.dump("empty")) == []


def test_wrong_rows_are_announced_on_entering_and_leaving(program, gcc_world):
    r = gcc_world.run(program, [
        # A valid row says nothing
        call("focus", "v4"), call("focus", "number"),
        *fill({"v4": "10.0", "mask": "40"}),
        call("focus", "v4"), call("focus", "mask"), call("focus", "number"),
        call("announced")])
    # High priority (GTK_ACCESSIBLE_ANNOUNCEMENT_PRIORITY_HIGH = 2); the address row on
    # entering and leaving, the netmask row as well
    assert r.value("announced") == [
        "Address|IP address value not valid|2",
        "Address|IP address value not valid|2",
        "Netmask|Netmask value not valid|2",
        "Netmask|Netmask value not valid|2",
    ]


@pytest.mark.parametrize("label, title", [
    ("IPv_4 Method", "IPv4 Method"),
    ("_DNS", "DNS"),
    ("Snake__case _Routes", "Snake_case Routes"),
    ("No mnemonic", "No mnemonic"),
    ("Tom & <Jerry>_", "Tom &amp; &lt;Jerry&gt;"),
    ("", ""),
])
def test_group_title_from_a_mnemonic_label(program, gcc_world, label, title):
    r = gcc_world.run(program, [call("title", label)])
    assert r.value("title") == title


def test_wrong_arguments_are_refused(program, gcc_world):
    r = gcc_world.run(program, [call("bad_family"), call("not_a_row")])
    assert r.value("bad_family") is True
    # Neither empty nor valid, no prefix: GLib says why
    assert r.value("not_a_row") == [False, False, 0]
    crit = [m["message"] for m in r.warnings()]
    assert len(crit) == 4
    assert "family == AF_INET || family == AF_INET6" in crit[0]
    assert all("ADW_IS_ENTRY_ROW (row)" in m for m in crit[1:])
