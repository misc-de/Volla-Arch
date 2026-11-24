# DNS Filter on the Network page (cc-plinius-dnsfilter.c): the group and its pages against a fake
# plinius-dnsfilter (org.plinius.DnsFilter on the test's system bus, its interface taken from the
# service's C source): what the pages show for the service's properties and answers, and which
# calls and property changes a tap sends.

import pytest

from conftest import PKG
from gcc_harness import (apply, back, build_panel, call, click, dump, enter, gcc_session,  # noqa: F401
                         gcc_world, interface_xml, pop_to, push, select, set_active, set_text,
                         settle, wait, wait_for)

NAME = "org.plinius.DnsFilter"
PATH = "/org/plinius/DnsFilter"
XML = interface_xml(PKG / "plinius-dnsfilter" / "plinius-dnsfilter.c", NAME)
PROPS = "org.freedesktop.DBus.Properties"

# A day long ago (UTC, the program's zone): 2026-01-02 03:04:00
OLD = 1767323040
# GLib pads %e with a figure space
OLD_SHOWN = "\u20072 Jan, 03:04"

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-dnsfilter.h"

void
gcc_test_setup (GccTest *t)
{
    gcc_test_add (t, cc_plinius_dnsfilter_group_new (t->panel));
}

/* The Network panel made anew: a second group */
gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (!g_str_equal (name, "group"))
        return FALSE;
    gcc_test_add (t, cc_plinius_dnsfilter_group_new (t->panel));
    json_builder_add_null_value (result);
    return TRUE;
}
'''


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("dnsfilter"), "dnsfilter",
                       ["cc-plinius-dnsfilter.c"], GLUE,
                       replacements={"cc-plinius-dnsfilter.c": [
                           ('"/usr/bin/plinius-dnsfilter"', '"{root}/usr/bin/plinius-dnsfilter"')]})


def installed(program):
    exe = program.root / "usr/bin/plinius-dnsfilter"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)


@pytest.fixture
def world(program, gcc_world):
    installed(program)
    return gcc_world


def service(world, own=True, **kw):
    """The fake service; properties as keyword arguments (GVariant text), replies= for methods."""
    replies = kw.pop("replies", {})
    world.service("system", NAME, PATH, XML, properties=kw, replies=replies, own=own)


def calls(world, member=None):
    return [(c["member"], c["args"]) for c in world.calls(NAME, member)]


def sets(world):
    """The properties the panel set, in order: [(property, value)]."""
    return [(c["args"][1], c["args"][2]) for c in world.calls(NAME, "Set")]


def stats(queries=0, blocked=0, average=0, **more):
    items = [f"'queries': <uint64 {queries}>", f"'blocked': <uint64 {blocked}>",
             f"'average-us': <uint32 {average}>"]
    items += [f"'{k.replace('_', '-')}': <{v}>" for k, v in more.items()]
    return f"({{{', '.join(items)}}},)"


def lst(id, name, enabled=True, rules=0, updated=0, url="https://lists.example/x.txt",
        downloading=False, error=None):
    items = [f"'id': <{id!r}>", f"'name': <{name!r}>", f"'url': <{url!r}>",
             f"'enabled': <{str(enabled).lower()}>", f"'rules': <uint32 {rules}>",
             f"'updated': <int64 {updated}>", f"'downloading': <{str(downloading).lower()}>"]
    if error is not None:
        items.append(f"'error': <{error!r}>")
    return "{" + ", ".join(items) + "}"


def lists(*items):
    return f"[{', '.join(items)}]" if items else "@aa{sv} []"


def entry(id, name, time_s=OLD, status=0, reason="", via="", app="", kind="phone", client="",
          type=1, answer="192.0.2.1", upstream="9.9.9.9", duration=1500, cached=False):
    """One log entry, plinius-dnsfilter's LOG_TYPE (uxsqssussssuqbs)."""
    return (f"(uint32 {id}, int64 {time_s * 1000000}, {name!r}, uint16 {type}, {kind!r}, "
            f"{client!r}, uint32 {status}, {reason!r}, {via!r}, {answer!r}, {upstream!r}, "
            f"uint32 {duration}, uint16 0, {str(cached).lower()}, {app!r})")


def entries(*items):
    return f"[{', '.join(items)}]" if items else "@a(uxsqssussssuqbs) []"


def names_reply(*items):
    """GetLogNames: [(entry, count, blocked)]."""
    body = ", ".join(f"({e}, uint32 {n}, uint32 b)".replace(" b)", f" {b})") for e, n, b in items)
    return f"([{body}],)" if items else "(@a((uxsqssussssuqbs)uu) [],)"


def network_row(screen):
    return screen.one(type="CcListRow", text="DNS Filter")


def value_of(screen, title, within=None):
    """The value label of a row with title (the numbers' rows)."""
    rows = screen.find(type="AdwActionRow", text=title)
    if within is not None:
        rows = [r for r in screen.within(type="AdwPreferencesGroup", text=within).find(
            type="AdwActionRow", text=title)]
    assert len(rows) == 1, rows
    return [c["text"] for c in rows[0].walk() if c.get("type") == "GtkLabel"][0]


# ----- The group on the Network page -----

def test_nothing_without_the_service(program, gcc_world):
    exe = program.root / "usr/bin/plinius-dnsfilter"
    if exe.exists():
        exe.unlink()
    r = gcc_world.run(program, [settle(), dump("page")])
    assert not r.dump("page").has(type="AdwPreferencesGroup", text="DNS Filter")
    assert not gcc_world.calls()


@pytest.mark.parametrize("status, shown", [("active", "On"), ("paused", "Paused"),
                                           ("no-network", "No Network"),
                                           ("error", "Not Working"), ("", "Off")])
def test_the_rows_label_says_the_filters_state(world, program, status, shown):
    service(world, Status=repr(status), Enabled="true")
    r = world.run(program, [settle(), dump("page")])
    row = network_row(r.dump("page"))
    assert row["subtitle"] == "Filter ads, trackers and malware"
    assert row["target"] == "'plinius-dnsfilter'"
    assert [c["text"] for c in row["children"] if c["type"] == "GtkLabel"] == [shown]
    assert not r.warnings()


def test_without_an_answer_the_row_stays_off(world, program):
    service(world, own=False)
    r = world.run(program, [settle(), dump("page"), push("plinius-dnsfilter"), settle(),
                            dump("main")])
    assert [c["text"] for c in network_row(r.dump("page"))["children"]
            if c["type"] == "GtkLabel"] == ["Off"]
    # Nothing to ask without a service: no call goes out
    assert not world.calls(NAME)
    assert r.dump("main").one(type="AdwSwitchRow")["active"] is False


# ----- Main page -----

def test_main_page_shows_state_numbers_and_summaries(world, program):
    service(world, Status="'active'", Enabled="true", RuleCount="uint32 1", CustomServers="true",
            Lists=lists(lst("a", "List A", rules=1200), lst("b", "List B", rules=34),
                        lst("c", "List C", enabled=False, rules=99)),
            replies={"GetStats": stats(1234, 56, 2500)})
    r = world.run(program, [settle(), push("plinius-dnsfilter"), settle(), dump("main")])
    s = r.dump("main")
    switch = s.one(type="AdwSwitchRow", text="Block Ads and Trackers")
    assert switch["active"] is True
    assert switch["subtitle"] == "For every connection, the hotspot and USB devices included"
    pause = s.one(type="AdwActionRow", text="Pause")
    assert pause["subtitle"] == "Lets everything through for a while"
    # The menu button to pause, not Resume
    assert s.has(type="GtkMenuButton", text="Off") and not s.has(type="GtkButton", text="Resume")
    assert value_of(s, "Queries") == "1234"
    assert value_of(s, "Blocked") == "56 (5 %)"
    assert value_of(s, "Average Response Time") == "2.5 ms"
    assert s.one(type="CcListRow", text="Blocklists")["subtitle"] == "2 lists, 1234 rules"
    assert s.one(type="CcListRow", text="Own Rules")["subtitle"] == "1 rule"
    assert s.one(type="CcListRow", text="DNS Servers")["subtitle"] == "Your servers"
    assert s.has(type="GtkDrawingArea")
    assert ("GetStats", [1]) in calls(world)
    # The page keeps the service while it is shown
    assert calls(world, "Watch") == [("Watch", [])]


@pytest.mark.parametrize("average, shown", [(0, "–"), (9999, "10.0 ms"), (10000, "10 ms"),
                                            (25600, "26 ms")])
def test_response_time(world, program, average, shown):
    service(world, Enabled="true", replies={"GetStats": stats(0, 0, average)})
    r = world.run(program, [settle(), push("plinius-dnsfilter"), settle(), dump("main")])
    s = r.dump("main")
    assert value_of(s, "Average Response Time") == shown
    # Without queries no share
    assert value_of(s, "Blocked") == "0"


def test_summaries_in_the_plural_and_from_the_network(world, program):
    service(world, Enabled="true", RuleCount="uint32 3", Lists=lists(lst("a", "A", rules=5)))
    r = world.run(program, [settle(), push("plinius-dnsfilter"), settle(), dump("main")])
    s = r.dump("main")
    assert s.one(type="CcListRow", text="Blocklists")["subtitle"] == "1 list, 5 rules"
    assert s.one(type="CcListRow", text="Own Rules")["subtitle"] == "3 rules"
    assert s.one(type="CcListRow", text="DNS Servers")["subtitle"] == "From the network"


def test_off_hides_the_pause(world, program):
    service(world, Enabled="false", Status="'off'")
    r = world.run(program, [settle(), push("plinius-dnsfilter"), settle(), dump("main")])
    s = r.dump("main")
    assert s.one(type="AdwSwitchRow", text="Block Ads and Trackers")["active"] is False
    assert not s.has(type="AdwActionRow", text="Pause")


def test_paused_shows_until_when_and_resume(world, program):
    service(world, Enabled="true", Status="'paused'", PausedUntil=f"int64 {OLD}")
    r = world.run(program, [settle(), push("plinius-dnsfilter"), settle(), dump("main"),
                            click(type="GtkButton", text="Resume"), settle()])
    s = r.dump("main")
    assert s.one(type="AdwActionRow", text="Pause")["subtitle"] == f"Paused until {OLD_SHOWN}"
    assert not s.has(type="GtkMenuButton", text="Off")
    assert calls(world, "Pause") == [("Pause", [0])]


def test_error_says_the_network_dns_is_used(world, program):
    service(world, Enabled="true", Status="'error'")
    r = world.run(program, [settle(), push("plinius-dnsfilter"), settle(), dump("main")])
    assert r.dump("main").one(type="AdwActionRow", text="Pause")["subtitle"] == (
        "The filter could not start, the phone uses the network's DNS")


def test_switch_sets_enabled(world, program):
    service(world, Enabled="true")
    world.run(program, [settle(), push("plinius-dnsfilter"), settle(),
                        set_active(False, type="AdwSwitchRow", text="Block Ads and Trackers"),
                        settle()])
    assert sets(world) == [("Enabled", False)]


@pytest.mark.parametrize("seconds", [60, 600, 3600, 28800])
def test_pause_for_a_while(world, program, seconds):
    service(world, Enabled="true")
    world.run(program, [settle(), push("plinius-dnsfilter"), settle(),
                        {"op": "action", "target": {"type": "AdwActionRow", "text": "Pause"},
                         "name": "dnsfilter.pause", "value": f"uint32 {seconds}"}, settle()])
    assert calls(world, "Pause") == [("Pause", [seconds])]


def test_pause_menu_offers_four_durations(world, program):
    service(world, Enabled="true")
    r = world.run(program, [settle(), push("plinius-dnsfilter"), settle(),
                            click(type="GtkMenuButton", text="Off"), settle(), dump("menu")])
    texts = r.dump("menu").texts()
    for item in ("For 1 Minute", "For 10 Minutes", "For 1 Hour", "For 8 Hours"):
        assert item in texts


def test_leaving_the_pages_lets_the_service_go(world, program):
    service(world, Enabled="true")
    world.run(program, [settle(), push("plinius-dnsfilter"), settle(), push("plinius-dnsfilter-settings"),
                        settle(), back(), settle(), back(), settle()])
    assert [m for m, _ in calls(world) if m in ("Watch", "Unwatch")] == ["Watch", "Unwatch"]


def test_service_changes_show_at_once(world, program):
    service(world, Enabled="false", Status="'off'")
    ctl = world.control()
    r = world.run(program, [settle(), push("plinius-dnsfilter"), settle(),
                            ctl.set_property(PATH, NAME, "Enabled", "true"),
                            ctl.set_property(PATH, NAME, "Status", "'active'"), settle(),
                            dump("main"), back(), settle(), dump("page")])
    s = r.dump("main")
    assert s.one(type="AdwSwitchRow", text="Block Ads and Trackers")["active"] is True
    assert s.has(type="AdwActionRow", text="Pause")
    assert [c["text"] for c in network_row(r.dump("page"))["children"]
            if c["type"] == "GtkLabel"] == ["On"]
    # Showing what the service says is no change of the panel's own
    assert sets(world) == []


def test_service_started_again_is_read_anew(world, program):
    service(world, Enabled="true", Status="'active'")
    ctl = world.control()
    world.run(program, [settle(), ctl.release(NAME), settle(), ctl.own(NAME), settle()])
    # Leaving changes nothing; coming back loads everything anew
    assert not world.calls_after("Release", name=NAME)[:-len(world.calls_after("Own", name=NAME))]
    again = {c["member"] for c in world.calls_after("Own", name=NAME)}
    assert {"GetRules", "GetStats"} <= again


# ----- Blocklists -----

def open_page(tag):
    return [settle(), push("plinius-dnsfilter"), settle(), push(tag), settle()]


def test_lists_with_their_state(world, program):
    service(world, Enabled="true", Lists=lists(
        lst("a", "Hagezi Pro", rules=207301, updated=OLD, url="https://a.example/pro.txt"),
        lst("b", "Fresh", downloading=True),
        lst("c", "Broken", enabled=False, error="HTTP 404", updated=OLD),
        lst("d", "Waiting", rules=0, updated=0),
        lst("e", "Single", rules=1, updated=OLD)))
    r = world.run(program, [*open_page("plinius-dnsfilter-lists"), dump("lists")])
    group = r.dump("lists").within(type="AdwPreferencesGroup", text="Subscribed Lists")
    rows = group.find(type="AdwActionRow")
    assert [(x["text"], x["subtitle"]) for x in rows] == [
        ("Hagezi Pro", f"207301 rules, updated {OLD_SHOWN}"),
        ("Fresh", "Downloading…"),
        ("Broken", "Not updated: HTTP 404"),
        ("Waiting", "Not downloaded yet"),
        ("Single", f"1 rule, updated {OLD_SHOWN}"),
    ]
    assert rows[0]["tooltip"] == "https://a.example/pro.txt"
    assert [n["active"] for n in group.find(type="GtkSwitch")] == [True, True, False, True, True]
    assert len(group.find(type="GtkButton", tooltip="Remove List")) == 5
    # Showing the lists switches nothing
    assert not calls(world, "SetListEnabled")


def test_no_lists_says_how_to_block(world, program):
    service(world, Enabled="true")
    r = world.run(program, [*open_page("plinius-dnsfilter-lists"), dump("lists")])
    g = r.dump("lists").one(type="AdwPreferencesGroup", text="Subscribed Lists")
    assert g["subtitle"] == "No lists. Add one to block anything."


def test_list_switch_and_remove(world, program):
    service(world, Enabled="true", Lists=lists(lst("a", "A"), lst("b", "B", enabled=False)))
    world.run(program, [*open_page("plinius-dnsfilter-lists"),
                        set_active(False, type="GtkSwitch", within={"type": "AdwActionRow", "text": "A"}),
                        set_active(True, type="GtkSwitch", within={"type": "AdwActionRow", "text": "B"}),
                        click(type="GtkButton", tooltip="Remove List",
                              within={"type": "AdwActionRow", "text": "B"}), settle()])
    assert calls(world, "SetListEnabled") == [("SetListEnabled", ["a", False]),
                                              ("SetListEnabled", ["b", True])]
    assert calls(world, "RemoveList") == [("RemoveList", ["b"])]


def test_lists_follow_the_service(world, program):
    service(world, Enabled="true", Lists=lists(lst("a", "A")))
    ctl = world.control()
    r = world.run(program, [*open_page("plinius-dnsfilter-lists"),
                            ctl.set_property(PATH, NAME, "Lists", lists(lst("a", "A"), lst("n", "New"))),
                            settle(), dump("lists")])
    group = r.dump("lists").within(type="AdwPreferencesGroup", text="Subscribed Lists")
    assert [x["text"] for x in group.find(type="AdwActionRow")] == ["A", "New"]


@pytest.mark.parametrize("hours, index, item", [(0, 0, "Never"), (6, 1, "Every 6 Hours"),
                                                (12, 2, "Every 12 Hours"), (24, 3, "Daily"),
                                                (72, 4, "Every 3 Days"), (168, 5, "Weekly"),
                                                (5, 3, "Daily")])
def test_update_interval_shown(world, program, hours, index, item):
    service(world, Enabled="true", UpdateInterval=f"uint32 {hours}")
    r = world.run(program, [*open_page("plinius-dnsfilter-lists"), dump("lists")])
    row = r.dump("lists").one(type="AdwComboRow", text="Update Lists")
    assert (row["selected"], row["selected_text"]) == (index, item)


def test_updates_settings_and_update_now(world, program):
    service(world, Enabled="true", UpdateInterval="uint32 24")
    world.run(program, [*open_page("plinius-dnsfilter-lists"),
                        select(5, type="AdwComboRow", text="Update Lists"),
                        set_active(True, type="AdwSwitchRow", text="Also on Mobile Data"),
                        click(type="AdwButtonRow", text="Update Now"), settle()])
    assert sets(world) == [("UpdateInterval", 168), ("UpdateOnMobileData", True)]
    assert calls(world, "UpdateLists") == [("UpdateLists", [])]


CATALOG = ("([{'id': <'hagezi-pro'>, 'name': <'Hagezi Pro'>, "
           "'description': <'Stricter; now and then a site needs an exception'>, 'added': <true>}, "
           "{'id': <'oisd'>, 'name': <'OISD'>, 'description': <'Ads and tracking'>, "
           "'added': <false>}],)")


def test_suggested_lists_and_adding_one(world, program):
    service(world, Enabled="true", replies={"GetCatalog": CATALOG})
    r = world.run(program, [*open_page("plinius-dnsfilter-lists"),
                            click(type="GtkButton", tooltip="Add List"), settle(), dump("add"),
                            click(type="GtkButton", text="Add"), settle(), dump("added")])
    group = r.dump("add").within(type="AdwPreferencesGroup", text="Suggested Lists")
    assert [(x["text"], x["subtitle"]) for x in group.find(type="AdwActionRow")] == [
        ("Hagezi Pro", "Stricter; now and then a site needs an exception"),
        ("OISD", "Ads and tracking")]
    # Subscribed: a check mark; not yet: Add
    assert group.within(type="AdwActionRow", text="Hagezi Pro").has(type="GtkImage",
                                                                    tooltip="Subscribed")
    assert calls(world, "AddList") == [("AddList", ["oisd", ""])]
    assert r.dump("added").one(type="GtkButton", text="Add").get("sensitive") is False
    assert ("GetCatalog", []) in calls(world)


def test_own_list_added_returns_to_the_lists(world, program):
    service(world, Enabled="true")
    r = world.run(program, [*open_page("plinius-dnsfilter-lists"),
                            click(type="GtkButton", tooltip="Add List"), settle(),
                            set_text("My List", type="AdwEntryRow", text="Name"),
                            set_text("https://example.org/hosts", type="AdwEntryRow",
                                     text="Web Address"),
                            click(type="AdwButtonRow", text="Add List"), settle(), dump("after")])
    assert calls(world, "AddList") == [("AddList", ["https://example.org/hosts", "My List"])]
    s = r.dump("after")
    # Back on the lists, the fields empty for the next time
    assert s.has(type="AdwNavigationPage", tag="plinius-dnsfilter-lists")
    assert not s.has(type="AdwNavigationPage", tag="plinius-dnsfilter-add-list")


def test_own_list_without_address_is_not_sent(world, program):
    service(world, Enabled="true")
    world.run(program, [*open_page("plinius-dnsfilter-add-list"),
                        set_text("My List", type="AdwEntryRow", text="Name"),
                        click(type="AdwButtonRow", text="Add List"), settle()])
    assert not calls(world, "AddList")


def test_own_list_refused_says_why(world, program):
    service(world, Enabled="true", replies={"AddList": {
        "error": "org.plinius.DnsFilter.Error.Invalid", "message": "Not a web address"}})
    r = world.run(program, [*open_page("plinius-dnsfilter-add-list"),
                            set_text("ftp://x", type="AdwEntryRow", text="Web Address"),
                            click(type="AdwButtonRow", text="Add List"), settle(), dump("after")])
    s = r.dump("after")
    assert s.one(type="AdwPreferencesGroup", text="Other List")["subtitle"] == "Not a web address"
    assert s.one(type="AdwEntryRow", text="Web Address")["value"] == "ftp://x"


# ----- Own rules -----

RULES = ("(['||ads.example^', '|exact.example^', '@@||cdn.example^', '@@|one.example^', "
         "'||track.example^$app=app/org.mozilla.firefox'],)")


def test_own_rules_by_kind(world, program):
    service(world, Enabled="true", replies={"GetRules": RULES})
    r = world.run(program, [*open_page("plinius-dnsfilter-rules"), dump("rules")])
    group = r.dump("rules").within(type="AdwPreferencesGroup", text="Your Rules")
    assert [(x["text"], x["subtitle"]) for x in group.find(type="AdwActionRow")] == [
        ("ads.example", "Blocked, with subdomains"),
        ("exact.example", "Blocked, this name only"),
        ("cdn.example", "Allowed, with subdomains"),
        ("one.example", "Allowed, this name only"),
        ("track.example", "Blocked, with subdomains · only org.mozilla.firefox"),
    ]
    icons = [i["icon"] for i in group.find(type="GtkImage") if i["icon"] != "user-trash-symbolic"]
    assert icons == ["action-unavailable-symbolic", "action-unavailable-symbolic",
                     "object-select-symbolic", "object-select-symbolic",
                     "action-unavailable-symbolic"]


def test_app_rule_names_the_app(world, program):
    (world.data_dir / "applications" / "org.mozilla.firefox.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Firefox\nExec=/bin/true\n")
    service(world, Enabled="true",
            replies={"GetRules": "(['@@||track.example^$app=app/org.mozilla.firefox'],)"})
    r = world.run(program, [*open_page("plinius-dnsfilter-rules"), dump("rules")])
    assert r.dump("rules").one(type="AdwActionRow", text="track.example")["subtitle"] == (
        "Allowed, with subdomains · only Firefox")


def test_no_rules_says_where_to_add_them(world, program):
    service(world, Enabled="true", replies={"GetRules": "(@as [],)"})
    r = world.run(program, [*open_page("plinius-dnsfilter-rules"), dump("rules")])
    assert r.dump("rules").one(type="AdwPreferencesGroup", text="Your Rules")["subtitle"] == (
        "No rules yet. Add one here, or block and unblock names right in the query log.")


def test_remove_rule_reads_the_rules_again(world, program):
    service(world, Enabled="true", replies={"GetRules": RULES})
    world.run(program, [*open_page("plinius-dnsfilter-rules"),
                        click(type="GtkButton", tooltip="Remove Rule",
                              within={"type": "AdwActionRow", "text": "cdn.example"}), settle()])
    assert calls(world, "RemoveRule") == [("RemoveRule", ["@@||cdn.example^"])]
    removed = [i for i, c in enumerate(calls(world)) if c[0] == "RemoveRule"][0]
    assert "GetRules" in [m for m, _ in calls(world)[removed:]]


@pytest.mark.parametrize("typed, kind, subdomains, rule", [
    ("example.com", None, True, "||example.com^"),
    ("example.com", "allow", True, "@@||example.com^"),
    ("example.com", None, False, "|example.com^"),
    ("example.com", "allow", False, "@@|example.com^"),
    ("  https://www.example.com/some/page?x=1", None, True, "||www.example.com^"),
    ("bücher.de", None, True, "||xn--bcher-kva.de^"),
    ("||as.written^$important", "allow", False, "||as.written^$important"),
    ("@@|kept.example^", None, True, "@@|kept.example^"),
])
def test_new_rule(world, program, typed, kind, subdomains, rule):
    service(world, Enabled="true")
    steps = [*open_page("plinius-dnsfilter-rules")]
    if kind:
        steps.append(select(name=kind, type="AdwToggleGroup",
                            within={"type": "AdwPreferencesGroup", "text": "New Rule"}))
    if not subdomains:
        steps.append(set_active(False, type="AdwSwitchRow", text="Include Subdomains"))
    steps += [set_text(typed, type="AdwEntryRow", text="Domain"), enter(type="AdwEntryRow", text="Domain"),
              settle(), dump("after")]
    r = world.run(program, steps)
    assert calls(world, "AddRule") == [("AddRule", [rule])]
    # Added: the field is empty again, everything read anew
    assert r.dump("after").one(type="AdwEntryRow", text="Domain")["value"] == ""


def test_empty_rule_is_not_sent(world, program):
    service(world, Enabled="true")
    world.run(program, [*open_page("plinius-dnsfilter-rules"),
                        set_text("   ", type="AdwEntryRow", text="Domain"),
                        click(type="AdwButtonRow", text="Add Rule"), settle()])
    assert not calls(world, "AddRule")


def test_refused_rule_marks_the_field(world, program):
    service(world, Enabled="true", replies={"AddRule": {
        "error": "org.plinius.DnsFilter.Error.Invalid", "message": "Not a domain"}})
    r = world.run(program, [*open_page("plinius-dnsfilter-rules"),
                            set_text("bad..name", type="AdwEntryRow", text="Domain"),
                            click(type="AdwButtonRow", text="Add Rule"), settle(), dump("after"),
                            {"op": "action", "name": "navigation.pop"}, settle()])
    s = r.dump("after")
    field = s.one(type="AdwEntryRow", text="Domain")
    assert field["value"] == "bad..name" and "error" in field["classes"]
    group = [g for g in s.find(type="AdwPreferencesGroup") if g.get("subtitle") == "Not a domain"]
    assert len(group) == 1


# ----- DNS servers -----

def test_servers_from_the_network_and_own(world, program):
    service(world, Enabled="true", CustomServers="true", LocalViaNetwork="true",
            NetworkServers="['192.168.1.1', 'fd00::1']",
            Servers="['9.9.9.9', 'tls://dns.quad9.net', 'https://cloudflare-dns.com/dns-query']")
    r = world.run(program, [*open_page("plinius-dnsfilter-servers"), dump("servers")])
    s = r.dump("servers")
    assert s.one(type="AdwActionRow", text="From the Network")["subtitle"] == "192.168.1.1, fd00::1"
    checks = s.find(type="GtkCheckButton")
    assert [c["active"] for c in checks] == [False, True]
    own = s.within(type="AdwPreferencesGroup", text="Your Servers")
    assert [(x["text"], x["subtitle"]) for x in own.find(type="AdwActionRow")] == [
        ("9.9.9.9", "Plain DNS"), ("tls://dns.quad9.net", "DNS over TLS"),
        ("https://cloudflare-dns.com/dns-query", "DNS over HTTPS")]
    # The field to add one stays the last row
    assert [x["type"] for x in own.find(type=("AdwActionRow", "AdwEntryRow"))][-1] == "AdwEntryRow"
    sugg = s.within(type="AdwPreferencesGroup", text="Suggestions")
    titles = [(x["text"], x["subtitle"]) for x in sugg.find(type="AdwActionRow")]
    # The ones in use are not suggested again
    assert ("Quad9, DNS over TLS", "tls://dns.quad9.net") not in titles
    assert ("Cloudflare, DNS over HTTPS", "https://cloudflare-dns.com/dns-query") not in titles
    assert ("Quad9, DNS over HTTPS", "https://dns.quad9.net/dns-query") in titles
    assert len(titles) == 6
    assert s.one(type="AdwSwitchRow", text="Local Names from the Network")["active"] is True


def test_no_network_right_now(world, program):
    service(world, Enabled="true")
    r = world.run(program, [*open_page("plinius-dnsfilter-servers"), dump("servers")])
    s = r.dump("servers")
    assert s.one(type="AdwActionRow", text="From the Network")["subtitle"] == "No network right now"
    assert [c["active"] for c in s.find(type="GtkCheckButton")] == [True, False]


def test_all_suggestions_in_use_hide_the_group(world, program):
    specs = ["tls://dns.quad9.net", "https://dns.quad9.net/dns-query", "tls://one.one.one.one",
             "https://cloudflare-dns.com/dns-query", "https://dns.mullvad.net/dns-query",
             "https://unfiltered.joindns4.eu/dns-query", "tls://dns3.digitalcourage.de",
             "tls://dns.google"]
    service(world, Enabled="true", CustomServers="true", Servers=repr(specs))
    r = world.run(program, [*open_page("plinius-dnsfilter-servers"), dump("servers")])
    assert not r.dump("servers").has(type="AdwPreferencesGroup", text="Suggestions")


def test_first_suggestion_switches_to_own_servers(world, program):
    service(world, Enabled="true")
    world.run(program, [*open_page("plinius-dnsfilter-servers"),
                        click(type="GtkButton", tooltip="Add Server",
                              within={"type": "AdwActionRow", "text": "Mullvad, DNS over HTTPS"}),
                        settle()])
    assert sets(world) == [("Servers", ["https://dns.mullvad.net/dns-query"]),
                           ("CustomServers", True)]


def test_typed_server_is_added_behind_the_others(world, program):
    service(world, Enabled="true", CustomServers="true", Servers="['9.9.9.9']")
    r = world.run(program, [*open_page("plinius-dnsfilter-servers"),
                            set_text("  tls://dns.example  ", type="AdwEntryRow", text="Add Server"),
                            apply(type="AdwEntryRow", text="Add Server"), settle(), dump("after")])
    assert sets(world) == [("Servers", ["9.9.9.9", "tls://dns.example"])]
    s = r.dump("after")
    assert s.one(type="AdwEntryRow", text="Add Server")["value"] == ""
    # The service's new list shows
    assert s.has(type="AdwActionRow", text="tls://dns.example", subtitle="DNS over TLS")


def test_server_already_there_or_empty_is_not_added(world, program):
    service(world, Enabled="true", CustomServers="true", Servers="['9.9.9.9']")
    world.run(program, [*open_page("plinius-dnsfilter-servers"),
                        set_text("9.9.9.9", type="AdwEntryRow", text="Add Server"),
                        apply(type="AdwEntryRow", text="Add Server"),
                        set_text("   ", type="AdwEntryRow", text="Add Server"),
                        apply(type="AdwEntryRow", text="Add Server"), settle()])
    assert sets(world) == []


def test_refused_server_says_why(world, program):
    service(world, Enabled="true", CustomServers="true", Servers="['9.9.9.9']")
    world.services[-1]["set_errors"] = {
        "Servers": ["org.plinius.DnsFilter.Error.Invalid", "Not a server: dns:"]}
    r = world.run(program, [*open_page("plinius-dnsfilter-servers"),
                            set_text("dns:", type="AdwEntryRow", text="Add Server"),
                            apply(type="AdwEntryRow", text="Add Server"), settle(), dump("after")])
    s = r.dump("after")
    field = s.one(type="AdwEntryRow", text="Add Server")
    assert field["value"] == "dns:" and "error" in field["classes"]
    assert s.one(type="AdwPreferencesGroup", text="Your Servers")["subtitle"] == "Not a server: dns:"


def test_remove_and_test_a_server(world, program):
    service(world, Enabled="true", CustomServers="true", Servers="['9.9.9.9', 'tls://x.example']",
            replies={"TestServer": ["(true, uint32 12, '')", "(false, uint32 0, 'No answer')"]})
    r = world.run(program, [*open_page("plinius-dnsfilter-servers"),
                            click(type="GtkButton", text="Test",
                                  within={"type": "AdwActionRow", "text": "9.9.9.9"}), settle(),
                            click(type="GtkButton", text="Test",
                                  within={"type": "AdwActionRow", "text": "tls://x.example"}),
                            settle(), dump("tested"),
                            click(type="GtkButton", tooltip="Remove Server",
                                  within={"type": "AdwActionRow", "text": "9.9.9.9"}), settle()])
    s = r.dump("tested")
    assert s.one(type="AdwActionRow", text="9.9.9.9")["subtitle"] == "Answers in 12 ms"
    assert s.one(type="AdwActionRow", text="tls://x.example")["subtitle"] == "Does not work: No answer"
    assert calls(world, "TestServer") == [("TestServer", ["9.9.9.9"]),
                                          ("TestServer", ["tls://x.example"])]
    assert sets(world) == [("Servers", ["tls://x.example"])]


def test_source_and_local_names(world, program):
    service(world, Enabled="true", Servers="['9.9.9.9']")
    world.run(program, [*open_page("plinius-dnsfilter-servers"),
                        click(type="AdwActionRow", text="Your Servers"),
                        set_active(True, type="AdwSwitchRow", text="Local Names from the Network"),
                        settle()])
    assert sets(world) == [("CustomServers", True), ("LocalViaNetwork", True)]


# ----- Settings -----

def test_settings_show_the_service(world, program):
    service(world, Enabled="true", BlockMode="'nxdomain'", SharedClients="true", RedirectDns="false",
            BlockEncryptedDns="true", KeepLog="true", LogSize="uint32 20000", StatsDays="uint32 90")
    r = world.run(program, [*open_page("plinius-dnsfilter-settings"), dump("settings")])
    s = r.dump("settings")
    assert s.one(type="AdwComboRow", text="Answer for Blocked Names")["selected_text"] == "Does Not Exist"
    assert [s.one(type="AdwSwitchRow", text=t)["active"] for t in
            ("Hotspot and USB Devices", "Apps with a Fixed DNS Server", "Refuse DNS over TLS",
             "Keep after Restart")] == [True, False, True, True]
    assert s.one(type="AdwComboRow", text="Keep the Last")["selected_text"] == "20,000 Queries"
    assert s.one(type="AdwComboRow", text="Keep For")["selected_text"] == "90 Days"
    # Showing them changes nothing
    assert sets(world) == []


def test_unknown_values_show_the_defaults(world, program):
    service(world, Enabled="true", BlockMode="'other'", LogSize="uint32 7", StatsDays="uint32 2")
    r = world.run(program, [*open_page("plinius-dnsfilter-settings"), dump("settings")])
    s = r.dump("settings")
    assert s.one(type="AdwComboRow", text="Answer for Blocked Names")["selected_text"] == "0.0.0.0"
    assert s.one(type="AdwComboRow", text="Keep the Last")["selected_text"] == "5,000 Queries"
    assert s.one(type="AdwComboRow", text="Keep For")["selected_text"] == "7 Days"


def test_settings_changes_go_to_the_service(world, program):
    service(world, Enabled="true", BlockMode="'null-ip'", LogSize="uint32 5000", StatsDays="uint32 7")
    world.run(program, [*open_page("plinius-dnsfilter-settings"),
                        select(2, type="AdwComboRow", text="Answer for Blocked Names"),
                        select(0, type="AdwComboRow", text="Keep the Last"),
                        select(3, type="AdwComboRow", text="Keep For"),
                        set_active(True, type="AdwSwitchRow", text="Hotspot and USB Devices"),
                        set_active(True, type="AdwSwitchRow", text="Apps with a Fixed DNS Server"),
                        set_active(True, type="AdwSwitchRow", text="Refuse DNS over TLS"),
                        set_active(True, type="AdwSwitchRow", text="Keep after Restart"), settle()])
    assert sets(world) == [("BlockMode", "refused"), ("LogSize", 1000), ("StatsDays", 90),
                           ("SharedClients", True), ("RedirectDns", True),
                           ("BlockEncryptedDns", True), ("KeepLog", True)]


@pytest.mark.parametrize("answer, cleared", [("Clear", True), ("Cancel", False)])
def test_clear_the_log_asks_first(world, program, answer, cleared):
    service(world, Enabled="true")
    r = world.run(program, [*open_page("plinius-dnsfilter-settings"),
                            click(type="AdwButtonRow", text="Clear Query Log"), settle(),
                            dump("asked"),
                            click(type="GtkButton", text=answer, within={"type": "AdwAlertDialog"}),
                            settle()])
    dialog = r.dump("asked").one(type="AdwAlertDialog")
    assert (dialog["text"], dialog["subtitle"]) == ("Clear Query Log?", "All recorded queries are deleted.")
    assert bool(calls(world, "ClearLog")) == cleared
    if cleared:
        # Everything is read again afterwards
        after = [m for m, _ in calls(world)][[m for m, _ in calls(world)].index("ClearLog"):]
        assert "GetRules" in after


# ----- Query log -----

ST_PASSED, ST_ALLOWED_RULE, ST_ALLOWED_LIST, ST_ALLOWED_SYSTEM, ST_BLOCKED_LIST, ST_BLOCKED_RULE, \
    ST_UNFILTERED, ST_FAILED, ST_BLOCKED_SPECIAL = range(9)
FIREFOX = "[Desktop Entry]\nType=Application\nName=Firefox\nExec=/bin/true\n"


def apps(world):
    (world.data_dir / "applications" / "org.mozilla.firefox.desktop").write_text(FIREFOX)


def log_rows(screen):
    page = screen.within(type="AdwNavigationPage", tag="plinius-dnsfilter-log")
    return [n for n in page.find(type="AdwActionRow")]


def test_log_names_with_who_asked_and_what_decided(world, program):
    apps(world)
    when = f"{OLD_SHOWN}"
    service(world, Enabled="true", replies={"GetLogNames": names_reply(
        (entry(9, "ads.example", status=ST_BLOCKED_LIST, reason="Hagezi", via="cdn.ads.example",
               app="app/org.mozilla.firefox"), 3, 3),
        (entry(8, "mixed.example", status=ST_PASSED, kind="hotspot", client="laptop"), 4, 1),
        (entry(7, "now.example", status=ST_PASSED, kind="usb", client="10.0.0.2"), 2, 1),
        (entry(6, "rule.example", status=ST_BLOCKED_RULE, reason="||rule.example^$app=app/x",
               app="android/uid_10123"), 1, 1),
        (entry(5, "fine.example", status=ST_ALLOWED_LIST, reason="Allowlist", app="system/geoclue.service",
               cached=True), 1, 0),
        (entry(4, "sys.example", status=ST_ALLOWED_SYSTEM, app="user/session.scope"), 1, 0),
        (entry(3, "k.example", status=ST_UNFILTERED, app="kernel"), 1, 0),
        (entry(2, "lost.example", status=ST_FAILED, app="system/foo-bar.service", answer=""), 1, 0),
        (entry(1, "doh.example", status=ST_BLOCKED_SPECIAL, app="android/other"), 1, 1),
        (entry(10, "x.example", status=ST_ALLOWED_RULE, reason="@@||x.example^", app="app/gone"), 2, 0),
        (entry(11, "odd.example", status=99, app="user/thing"), 1, 0))})
    r = world.run(program, [*open_page("plinius-dnsfilter-log"), dump("log")])
    rows = log_rows(r.dump("log"))
    assert [(x["text"], x["subtitle"]) for x in rows] == [
        ("ads.example", f"{when} · Firefox · 3 queries\nBlocked by Hagezi, via cdn.ads.example"),
        ("mixed.example", f"{when} · laptop (Hotspot) · 4 queries\nPartially blocked"),
        ("now.example", f"{when} · 10.0.0.2 (USB) · 2 queries\nPartially blocked"),
        ("rule.example", f"{when} · Android app 10123 · 1 query\nBlocked for this app"),
        ("fine.example", f"{when} · Location · 1 query\nAllowed by Allowlist"),
        ("sys.example", f"{when} · Session · 1 query\nNeeded by the phone"),
        ("k.example", f"{when} · Linux Kernel · 1 query\nNot filtered"),
        ("lost.example", f"{when} · foo-bar · 1 query\nNo answer"),
        ("doh.example", f"{when} · Android · 1 query\nBlocked: own encrypted DNS of the browser"),
        ("x.example", f"{when} · gone · 2 queries\nAllowed by your rule"),
        ("odd.example", f"{when} · thing · 1 query"),
    ]
    assert rows[0]["tooltip"] == "Answer: 192.0.2.1\nServer: 9.9.9.9\nResponse time: 1.5 ms"
    assert rows[4]["tooltip"] == "Answer: 192.0.2.1\nServer: from the cache\nResponse time: 1.5 ms"
    assert rows[7]["tooltip"].startswith("Answer: –\n")
    # Red only when every query was blocked
    red = [x["text"] for x in rows if "plinius-dns-blocked" in x.get("classes", [])]
    assert red == ["ads.example", "rule.example", "doh.example"]
    assert calls(world, "GetLogNames") == [("GetLogNames", [0, 150, "", 0])]
    # Fewer than a page: no older ones to show
    assert not r.dump("log").has(type="AdwButtonRow", text="Show Older Queries")


def test_log_blocked_by_a_count_not_the_last_status(world, program):
    service(world, Enabled="true", replies={"GetLogNames": names_reply(
        (entry(1, "b.example", status=ST_PASSED), 2, 2))})
    r = world.run(program, [*open_page("plinius-dnsfilter-log"), dump("log")])
    assert log_rows(r.dump("log"))[0]["subtitle"].endswith("\nBlocked")


def test_a_full_page_offers_older_names(world, program):
    page = [(entry(1000 - i, f"n{i}.example"), 1, 0) for i in range(150)]
    service(world, Enabled="true", replies={"GetLogNames": [names_reply(*page), names_reply(
        (entry(1, "old.example"), 1, 0), (entry(2, "n0.example"), 1, 0))]})
    r = world.run(program, [*open_page("plinius-dnsfilter-log"),
                            click(type="AdwButtonRow", text="Show Older Queries"), settle(),
                            dump("more")])
    assert calls(world, "GetLogNames") == [("GetLogNames", [0, 150, "", 0]),
                                           ("GetLogNames", [150, 150, "", 0])]
    rows = log_rows(r.dump("more"))
    # A name already listed is not listed twice
    assert len(rows) == 151 and rows[-1]["text"] == "old.example"
    assert not r.dump("more").has(type="AdwButtonRow", text="Show Older Queries")


def test_empty_log_explains_itself(world, program):
    service(world, Enabled="true", KeepLog="false")
    r = world.run(program, [*open_page("plinius-dnsfilter-log"), dump("empty"),
                            select(name="blocked", type="AdwToggleGroup",
                                   within={"type": "AdwNavigationPage",
                                           "tag": "plinius-dnsfilter-log"}),
                            settle(), dump("narrowed")])
    texts = r.dump("empty").texts()
    assert "No queries yet" in texts
    assert "The log starts anew with each restart; “Keep after Restart” in Settings keeps it" in texts
    assert "No matching names" in r.dump("narrowed").texts()
    assert calls(world, "GetLogNames")[-1] == ("GetLogNames", [0, 150, "", 1])


def test_kept_log_needs_no_hint(world, program):
    service(world, Enabled="true", KeepLog="true")
    r = world.run(program, [*open_page("plinius-dnsfilter-log"), dump("empty")])
    assert not any(t.startswith("The log starts anew") for t in r.dump("empty").texts())


def test_search_and_filter_ask_again(world, program):
    service(world, Enabled="true")
    world.run(program, [*open_page("plinius-dnsfilter-log"),
                        set_text("track", type="GtkSearchEntry"), settle(400),
                        select(name="allowed", type="AdwToggleGroup",
                               within={"type": "AdwNavigationPage", "tag": "plinius-dnsfilter-log"}),
                        settle()])
    assert calls(world, "GetLogNames")[1:] == [("GetLogNames", [0, 150, "track", 0]),
                                               ("GetLogNames", [0, 150, "track", 2])]


def test_live_queries_come_on_top(world, program):
    apps(world)
    service(world, Enabled="true", replies={"GetLogNames": names_reply(
        (entry(5, "a.example"), 1, 0), (entry(4, "b.example"), 1, 0))})
    ctl = world.control()
    logged = entries(entry(6, "b.example", status=ST_BLOCKED_LIST, reason="L"),
                     entry(7, "new.example", app="app/org.mozilla.firefox"))
    r = world.run(program, [*open_page("plinius-dnsfilter-log"),
                            ctl.emit(PATH, NAME, "Logged", f"({logged},)"), settle(), dump("live")])
    rows = log_rows(r.dump("live"))
    assert [x["text"] for x in rows] == ["new.example", "b.example", "a.example"]
    assert rows[1]["subtitle"] == f"{OLD_SHOWN} · This phone · 2 queries\nPartially blocked"
    assert rows[0]["subtitle"] == f"{OLD_SHOWN} · Firefox · 1 query"


@pytest.mark.parametrize("search, toggle, shown", [
    ("", "blocked", ["blk.example"]),
    ("", "allowed", ["ok.example", "fox.example"]),
    ("fire", "all", ["fox.example"]),
    ("ok", "all", ["ok.example"]),
    ("org.mozilla", "all", ["fox.example"]),
])
def test_live_queries_follow_search_and_filter(world, program, search, toggle, shown):
    apps(world)
    service(world, Enabled="true")
    ctl = world.control()
    logged = entries(entry(1, "blk.example", status=ST_BLOCKED_LIST, reason="L"),
                     entry(2, "ok.example"),
                     entry(3, "fox.example", app="app/org.mozilla.firefox"))
    steps = [*open_page("plinius-dnsfilter-log")]
    if search:
        steps += [set_text(search, type="GtkSearchEntry"), settle(400)]
    steps += [select(name=toggle, type="AdwToggleGroup",
                     within={"type": "AdwNavigationPage", "tag": "plinius-dnsfilter-log"}),
              settle(), ctl.emit(PATH, NAME, "Logged", f"({logged},)"), settle(), dump("live")]
    r = world.run(program, steps)
    assert [x["text"] for x in log_rows(r.dump("live"))] == list(reversed(shown))


def test_live_queries_need_an_open_log(world, program):
    service(world, Enabled="true")
    ctl = world.control()
    logged = entries(entry(1, "a.example"))
    r = world.run(program, [settle(), push("plinius-dnsfilter"), settle(),
                            ctl.emit(PATH, NAME, "Logged", f"({logged},)"), settle(),
                            push("plinius-dnsfilter-log"), settle(), dump("log")])
    # Only what GetLogNames gave (nothing): the signal came while the log was closed
    assert log_rows(r.dump("log")) == []


# ----- A name's page -----

def detail(name, status=ST_PASSED, reason="", via="", logged=(), askers="@a(su) []", checks=None,
           check_names=None):
    """Replies for a name's page."""
    replies = {"Check": f"(uint32 {status}, {reason!r})", "GetLog": f"({entries(*logged)},)",
               "GetAskersOf": f"({askers},)"}
    if check_names:
        replies["CheckNames"] = check_names
    return replies


def name_page(screen):
    return screen.within(type="AdwNavigationPage", tag="plinius-dnsfilter-name")


def test_tap_a_name_opens_its_page(world, program):
    apps(world)
    replies = detail("ads.example", status=ST_BLOCKED_LIST, reason="Hagezi", logged=[
        entry(9, "ads.example", status=ST_BLOCKED_LIST, reason="Hagezi", app="app/org.mozilla.firefox",
              type=28),
        entry(8, "ads.example", app="app/org.mozilla.firefox", answer="192.0.2.7", type=1),
        entry(7, "ads.example", kind="hotspot", client="laptop", type=65, answer=""),
        entry(6, "other.example")])
    replies["GetLogNames"] = names_reply((entry(9, "ads.example", status=ST_BLOCKED_LIST,
                                                reason="Hagezi"), 3, 2))
    replies["CheckNames"] = "([(uint32 4, 'Hagezi')],)"
    service(world, Enabled="true", StatsDays="uint32 7", replies=replies)
    r = world.run(program, [*open_page("plinius-dnsfilter-log"),
                            click(type="AdwActionRow", text="ads.example"), settle(), dump("name")])
    page = r.dump("name").one(type="AdwNavigationPage", tag="plinius-dnsfilter-name")
    assert page["text"] == "ads.example"
    s = name_page(r.dump("name"))
    head = s.find(type="AdwActionRow", text="ads.example")[0]
    assert head["subtitle"] == "Blocked by Hagezi"
    # Unblock in green
    assert "plinius-dns-unblock" in s.one(type="GtkButton", text="Unblock")["classes"]
    groups = s.find(type="AdwExpanderRow")
    assert [(g["text"], g["subtitle"]) for g in groups] == [
        ("Firefox", f"{OLD_SHOWN} · 2 queries\nBlocked by Hagezi"),
        ("laptop (Hotspot)", f"{OLD_SHOWN} · 1 query"),
    ]
    assert "plinius-dns-blocked" in groups[0]["classes"]
    assert calls(world, "GetLog") == [("GetLog", [0, 150, "ads.example", 4])]
    assert calls(world, "GetAskersOf") == [("GetAskersOf", [7, "ads.example"])]
    assert calls(world, "CheckNames") == [("CheckNames", ["app/org.mozilla.firefox", ["ads.example"]])]
    assert calls(world, "Check") == [("Check", ["ads.example"])]


def test_name_page_without_a_name_stays_empty(world, program):
    service(world, Enabled="true")
    world.run(program, [*open_page("plinius-dnsfilter-log"), push("plinius-dnsfilter-name"), settle()])
    assert not calls(world, "GetLog") and not calls(world, "Check")


def test_one_app_shows_its_queries_at_once(world, program):
    apps(world)
    replies = detail("ads.example", logged=[
        entry(9, "ads.example", status=ST_BLOCKED_LIST, reason="L", app="app/org.mozilla.firefox", type=28),
        entry(8, "ads.example", app="app/org.mozilla.firefox", answer="192.0.2.7", type=1),
        entry(7, "ads.example", app="app/org.mozilla.firefox", answer="", type=4242)],
        check_names="([(uint32 0, '')],)")
    replies["GetLogNames"] = names_reply((entry(9, "ads.example"), 3, 1))
    service(world, Enabled="true", replies=replies)
    r = world.run(program, [*open_page("plinius-dnsfilter-log"),
                            click(type="AdwActionRow", text="ads.example"), settle(), dump("name")])
    s = name_page(r.dump("name"))
    group = s.one(type="AdwExpanderRow", text="Firefox")
    assert group["expanded"] is True
    assert group["subtitle"] == f"{OLD_SHOWN} · 3 queries"
    inner = Screen_of(group)
    # (The expander's own header is an action row too)
    assert [(x["text"], x["subtitle"]) for x in inner.find(type="AdwActionRow")
            if x["text"] != "Firefox"] == [
        (OLD_SHOWN, "AAAA · Blocked by L"), (OLD_SHOWN, "A · Answer: 192.0.2.7"),
        (OLD_SHOWN, "TYPE4242 · Answer: –")]
    # Allowed for the app now: Block for App
    button = inner.one(type="GtkButton", text="Block for App")
    assert "destructive-action" in button["classes"] and button.get("sensitive", True)


def Screen_of(node):
    from gcc_harness import Screen
    return Screen(node.get("children", []))


@pytest.mark.parametrize("status, reason, label, rule", [
    (ST_PASSED, "", "Block Globally", ("AddRule", ["||ads.example^"])),
    (ST_ALLOWED_LIST, "Allow", "Block Globally", ("AddRule", ["||ads.example^"])),
    (ST_FAILED, "", "Block Globally", ("AddRule", ["||ads.example^"])),
    (ST_BLOCKED_LIST, "L", "Unblock", ("AddRule", ["@@||ads.example^"])),
    (ST_BLOCKED_RULE, "||ads.example^", "Unblock", ("RemoveRule", ["||ads.example^"])),
    (ST_ALLOWED_RULE, "@@||ads.example^", "Remove Exception", ("RemoveRule", ["@@||ads.example^"])),
])
def test_block_and_unblock_on_a_names_page(world, program, status, reason, label, rule):
    replies = detail("ads.example", status=status, reason=reason)
    replies["GetLogNames"] = names_reply((entry(9, "ads.example"), 1, 0))
    service(world, Enabled="true", replies=replies)
    r = world.run(program, [*open_page("plinius-dnsfilter-log"),
                            click(type="AdwActionRow", text="ads.example"), settle(),
                            click(type="GtkButton", text=label), settle(), dump("after")])
    assert calls(world, rule[0]) == [rule]
    # Checked again afterwards; the button can be used again once the service answered
    assert len(calls(world, "Check")) == 2
    assert name_page(r.dump("after")).one(type="GtkButton", text=label).get("sensitive", True)


@pytest.mark.parametrize("status", [ST_ALLOWED_SYSTEM, ST_UNFILTERED, ST_BLOCKED_SPECIAL])
def test_nothing_to_do_for_names_the_phone_needs(world, program, status):
    replies = detail("ads.example", status=status)
    replies["GetLogNames"] = names_reply((entry(9, "ads.example"), 1, 0))
    service(world, Enabled="true", replies=replies)
    r = world.run(program, [*open_page("plinius-dnsfilter-log"),
                            click(type="AdwActionRow", text="ads.example"), settle(), dump("name")])
    s = name_page(r.dump("name"))
    assert not s.find(type="GtkButton", text="Block Globally")
    assert not s.find(type="GtkButton", text="Unblock")


@pytest.mark.parametrize("now, label, rule", [
    ("(uint32 0, '')", "Block for App", "||ads.example^$app=app/org.mozilla.firefox"),
    ("(uint32 5, '||ads.example^$app=app/org.mozilla.firefox')", "Unblock for App",
     "@@||ads.example^$app=app/org.mozilla.firefox"),
])
def test_block_for_one_app(world, program, now, label, rule):
    apps(world)
    replies = detail("ads.example", logged=[entry(9, "ads.example", app="app/org.mozilla.firefox")],
                     check_names=f"([{now}],)")
    replies["GetLogNames"] = names_reply((entry(9, "ads.example"), 1, 0))
    service(world, Enabled="true", replies=replies)
    r = world.run(program, [*open_page("plinius-dnsfilter-log"),
                            click(type="AdwActionRow", text="ads.example"), settle(),
                            click(type="GtkButton", text=label), settle(), dump("after")])
    assert calls(world, "AddRule") == [("AddRule", [rule])]
    # The app's row is checked again, its button usable again
    assert len(calls(world, "CheckNames")) == 2
    assert name_page(r.dump("after")).one(type="GtkButton", text=label).get("sensitive", True)


def test_askers_from_the_statistics_only(world, program):
    apps(world)
    replies = detail("ads.example", askers="[('app/org.mozilla.firefox', uint32 12), "
                                           "('\\tusb\\t10.0.0.2', uint32 1)]",
                     check_names="([(uint32 0, '')],)")
    replies["GetLogNames"] = names_reply((entry(9, "ads.example"), 1, 0))
    service(world, Enabled="true", replies=replies)
    r = world.run(program, [*open_page("plinius-dnsfilter-log"),
                            click(type="AdwActionRow", text="ads.example"), settle(), dump("name")])
    s = name_page(r.dump("name"))
    assert [(g["text"], g["subtitle"]) for g in s.find(type="AdwExpanderRow")] == [
        ("Firefox", "12 queries"), ("10.0.0.2 (USB)", "1 query")]


def test_live_query_of_the_shown_name(world, program):
    apps(world)
    replies = detail("ads.example", logged=[entry(5, "ads.example")])
    replies["GetLogNames"] = names_reply((entry(5, "ads.example"), 1, 0))
    service(world, Enabled="true", replies=replies)
    ctl = world.control()
    logged = entries(entry(6, "ads.example", status=ST_BLOCKED_LIST, reason="L", kind="phone"),
                     entry(7, "other.example"))
    r = world.run(program, [*open_page("plinius-dnsfilter-log"),
                            click(type="AdwActionRow", text="ads.example"), settle(),
                            ctl.emit(PATH, NAME, "Logged", f"({logged},)"), settle(), dump("name"),
                            back(), settle(), dump("log")])
    group = name_page(r.dump("name")).one(type="AdwExpanderRow", text="This phone")
    assert group["subtitle"] == f"{OLD_SHOWN} · 2 queries\nPartially blocked"
    # Back on the list, which went on live below the name's page
    assert [x["text"] for x in log_rows(r.dump("log"))] == ["other.example", "ads.example"]


# ----- Statistics -----

FULL_STATS = stats(
    5000, 500, 12000, cached="uint64 1200", rules="uint32 207301",
    top_domains="[('a.example', uint32 300), ('b.example', uint32 20)]",
    top_blocked="[('ads.example', uint32 90)]",
    top_apps="[('app/org.mozilla.firefox', uint32 4000), ('app/gone', uint32 3)]",
    top_clients="[('phone\\t', uint32 4900), ('hotspot\\tlaptop', uint32 100)]",
    upstreams="[('9.9.9.9', uint32 10, uint32 30000), ('tls://dns.quad9.net', uint32 400, uint32 9000)]",
    series=f"[(int64 {OLD}, uint32 4, uint32 1, uint32 0, uint32 0)]")


def stats_page(screen):
    return screen.within(type="AdwNavigationPage", tag="plinius-dnsfilter-stats")


def test_statistics_numbers_and_tops(world, program):
    apps(world)
    service(world, Enabled="true", replies={"GetStats": FULL_STATS,
                                            "Check": "(uint32 4, 'L')"})
    r = world.run(program, [*open_page("plinius-dnsfilter-stats"), dump("stats")])
    s = stats_page(r.dump("stats"))
    assert value_of(s, "Queries") == "5000"
    assert value_of(s, "Blocked") == "500 (10 %)"
    assert value_of(s, "Answered from the Cache") == "1200"
    assert value_of(s, "Average Response Time") == "12 ms"
    assert value_of(s, "Rules of the Lists") == "207301"
    subs = {n["text"]: n["subtitle"] for n in s.find(type="CcListRow")}
    assert subs == {"Most Asked": "a.example · 300", "Most Blocked": "ads.example · 90",
                    "Apps": "Firefox · 4000", "Devices": "This phone · 4900",
                    # The server that answered most, though listed second
                    "DNS Servers": "tls://dns.quad9.net · 400"}
    assert s.one(type="AdwToggleGroup")["active_name"] == "day"
    assert ("GetStats", [1]) in calls(world)


def test_statistics_without_entries(world, program):
    service(world, Enabled="true", replies={"GetStats": stats(0, 0, 0)})
    r = world.run(program, [*open_page("plinius-dnsfilter-stats"), dump("stats")])
    subs = {n["text"]: n["subtitle"] for n in stats_page(r.dump("stats")).find(type="CcListRow")}
    assert set(subs.values()) == {"None"}


@pytest.mark.parametrize("tag, title, rows", [
    # What Check says now about each name, not what the list was
    ("plinius-dnsfilter-stats-asked", "Most Asked", [("a.example", "Blocked by L"),
                                                     ("b.example", "Blocked by L")]),
    ("plinius-dnsfilter-stats-blocked", "Most Blocked", [("ads.example", "Blocked by L")]),
    ("plinius-dnsfilter-stats-apps", "Apps", [("Firefox", None), ("gone", None)]),
    ("plinius-dnsfilter-stats-devices", "Devices", [("This phone", None), ("laptop (Hotspot)", None)]),
    ("plinius-dnsfilter-stats-servers", "DNS Servers", [("9.9.9.9", "10 answers, 30 ms"),
                                                        ("tls://dns.quad9.net", "400 answers, 9.0 ms")]),
])
def test_statistics_lists(world, program, tag, title, rows):
    apps(world)
    service(world, Enabled="true", replies={"GetStats": FULL_STATS, "Check": "(uint32 4, 'L')"})
    r = world.run(program, [*open_page("plinius-dnsfilter-stats"), push(tag), settle(), dump("list")])
    page = r.dump("list").within(type="AdwNavigationPage", tag=tag)
    assert page.one(type="AdwPreferencesGroup")["text"] == "Last 24 Hours"
    assert [(x["text"], x.get("subtitle")) for x in page.find(type="AdwActionRow")] == rows


@pytest.mark.parametrize("name, days, title", [("week", 7, "Last 7 Days"), ("month", 30, "Last 30 Days")])
def test_statistics_range(world, program, name, days, title):
    service(world, Enabled="true", replies={"GetStats": FULL_STATS})
    r = world.run(program, [*open_page("plinius-dnsfilter-stats"),
                            select(name=name, type="AdwToggleGroup",
                                   within={"type": "AdwNavigationPage", "tag": "plinius-dnsfilter-stats"}),
                            settle(), push("plinius-dnsfilter-stats-asked"), settle(), dump("list")])
    assert ("GetStats", [days]) in calls(world)
    page = r.dump("list").within(type="AdwNavigationPage", tag="plinius-dnsfilter-stats-asked")
    assert page.one(type="AdwPreferencesGroup")["text"] == title


@pytest.mark.parametrize("answer, cleared", [("Clear", True), ("Cancel", False)])
def test_clear_statistics_asks_first(world, program, answer, cleared):
    service(world, Enabled="true")
    r = world.run(program, [*open_page("plinius-dnsfilter-stats"),
                            click(type="AdwButtonRow", text="Clear Statistics"), settle(),
                            dump("asked"),
                            click(type="GtkButton", text=answer, within={"type": "AdwAlertDialog"}),
                            settle()])
    d = r.dump("asked").one(type="AdwAlertDialog")
    assert (d["text"], d["subtitle"]) == ("Clear Statistics?", "All counts are lost.")
    assert bool(calls(world, "ClearStats")) == cleared


def test_a_most_asked_name_opens_its_page_over_the_range(world, program):
    service(world, Enabled="true", replies={**detail("a.example"), "GetStats": FULL_STATS})
    r = world.run(program, [*open_page("plinius-dnsfilter-stats"),
                            select(name="week", type="AdwToggleGroup",
                                   within={"type": "AdwNavigationPage", "tag": "plinius-dnsfilter-stats"}),
                            settle(), push("plinius-dnsfilter-stats-asked"), settle(),
                            click(type="AdwActionRow", text="a.example"), settle(), dump("name")])
    assert r.dump("name").has(type="AdwNavigationPage", tag="plinius-dnsfilter-name", text="a.example")
    assert calls(world, "GetAskersOf") == [("GetAskersOf", [7, "a.example"])]


# ----- The names of one app or device -----

def test_names_an_app_asked(world, program):
    apps(world)
    service(world, Enabled="true", replies={
        "GetStats": FULL_STATS,
        "GetNamesOf": "([('ads.example', uint32 5), ('ok.example', uint32 1)],)",
        "CheckNames": "([(uint32 4, 'L'), (uint32 0, '')],)"})
    r = world.run(program, [*open_page("plinius-dnsfilter-stats"), push("plinius-dnsfilter-stats-apps"),
                            settle(), click(type="AdwActionRow", text="Firefox"), settle(), dump("who"),
                            click(type="GtkButton", text="Unblock for App"), settle()])
    page = r.dump("who").within(type="AdwNavigationPage", tag="plinius-dnsfilter-stats-who")
    blocked = page.within(type="AdwPreferencesGroup", text="Blocked")
    allowed = page.within(type="AdwPreferencesGroup", text="Allowed")
    assert [(x["text"], x["subtitle"]) for x in blocked.find(type="AdwActionRow")] == [
        ("ads.example", "5 queries\nBlocked by L")]
    assert [(x["text"], x["subtitle"]) for x in allowed.find(type="AdwActionRow")] == [
        ("ok.example", "1 query")]
    assert allowed.has(type="GtkButton", text="Block for App")
    assert calls(world, "GetNamesOf")[0] == ("GetNamesOf", [1, "app/org.mozilla.firefox"])
    assert calls(world, "CheckNames")[0] == ("CheckNames", ["app/org.mozilla.firefox",
                                                            ["ads.example", "ok.example"]])
    assert calls(world, "AddRule") == [("AddRule", ["@@||ads.example^$app=app/org.mozilla.firefox"])]
    # The page is read again for the new rule
    assert len(calls(world, "GetNamesOf")) == 2


def test_names_a_device_asked_block_for_all(world, program):
    service(world, Enabled="true", replies={
        "GetStats": FULL_STATS,
        "GetNamesOf": "([('ads.example', uint32 5)],)",
        "CheckNames": "([(uint32 0, '')],)"})
    r = world.run(program, [*open_page("plinius-dnsfilter-stats"), push("plinius-dnsfilter-stats-devices"),
                            settle(), click(type="AdwActionRow", text="laptop (Hotspot)"), settle(),
                            dump("who"), click(type="GtkButton", text="Block Globally"), settle()])
    page = r.dump("who").one(type="AdwNavigationPage", tag="plinius-dnsfilter-stats-who")
    assert page["text"] == "laptop (Hotspot)"
    assert calls(world, "GetNamesOf")[0] == ("GetNamesOf", [1, "\thotspot\tlaptop"])
    assert calls(world, "CheckNames")[0] == ("CheckNames", ["", ["ads.example"]])
    assert calls(world, "AddRule") == [("AddRule", ["||ads.example^"])]


def test_an_app_without_names_says_none(world, program):
    apps(world)
    service(world, Enabled="true", replies={
        "GetStats": FULL_STATS, "GetNamesOf": "(@a(su) [],)", "CheckNames": "(@a(us) [],)"})
    r = world.run(program, [*open_page("plinius-dnsfilter-stats"), push("plinius-dnsfilter-stats-apps"),
                            settle(), click(type="AdwActionRow", text="Firefox"), settle(), dump("who")])
    page = r.dump("who").within(type="AdwNavigationPage", tag="plinius-dnsfilter-stats-who")
    assert page.one(type="AdwPreferencesGroup", text="Allowed")["subtitle"] == "None"
    assert not page.has(type="AdwPreferencesGroup", text="Blocked")


def test_names_not_readable_say_none(world, program):
    apps(world)
    service(world, Enabled="true", replies={
        "GetStats": FULL_STATS,
        "GetNamesOf": {"error": "org.freedesktop.DBus.Error.AccessDenied", "message": "No"}})
    r = world.run(program, [*open_page("plinius-dnsfilter-stats"), push("plinius-dnsfilter-stats-apps"),
                            settle(), click(type="AdwActionRow", text="Firefox"), settle(), dump("who")])
    page = r.dump("who").within(type="AdwNavigationPage", tag="plinius-dnsfilter-stats-who")
    assert page.one(type="AdwPreferencesGroup", text="Allowed")["subtitle"] == "None"


# ----- Edges: no bus, answers that come late or not at all, many rows -----

def test_without_a_system_bus_nothing_is_sent(world, program):
    r = world.run(program, [settle(), push("plinius-dnsfilter"), settle(),
                            set_active(False, type="AdwSwitchRow", text="Block Ads and Trackers"),
                            settle(), dump("main")], system_bus=False)
    assert [m["message"].startswith("plinius-dnsfilter: ") for m in r.warnings()] == [True]
    assert not world.calls(NAME)
    # The page is there, with nothing in it from the service
    assert value_of(r.dump("main"), "Queries") == "–"


def test_a_call_still_on_its_way_when_the_panel_goes(world, program):
    service(world, Enabled="true", replies={"GetStats": {"delay_ms": 3000, "reply": stats(1)}})
    r = world.run(program, [settle(), push("plinius-dnsfilter"), settle()])
    # The answer never arrives: the panel is gone, its call cancelled, nothing warned
    assert not r.warnings()


def test_a_second_group_on_a_new_network_page(world, program):
    service(world, Enabled="true", Status="'active'")
    r = world.run(program, [settle(), call("group"), settle(), dump("page")])
    rows = r.dump("page").find(type="CcListRow", text="DNS Filter")
    assert len(rows) == 2


def test_today_shows_only_the_time(world, program):
    import time as _time
    now = int(_time.time())
    if 86400 - now % 86400 < 10:
        _time.sleep(11)
        now = int(_time.time())
    service(world, Enabled="true", replies={"GetLogNames": names_reply(
        (entry(2, "now.example", time_s=now), 1, 0),
        (entry(1, "far.example", time_s=2**40), 1, 0))})
    r = world.run(program, [*open_page("plinius-dnsfilter-log"), dump("log")])
    rows = log_rows(r.dump("log"))
    assert rows[0]["subtitle"].startswith(_time.strftime("%H:%M:%S", _time.gmtime(now)) + " · ")
    # A time GLib cannot show: nothing, not a crash
    assert rows[1]["subtitle"].startswith(" · This phone")


def test_devices_of_other_kinds_by_their_name(world, program):
    service(world, Enabled="true", replies={"GetLogNames": names_reply(
        (entry(1, "a.example", kind="vpn", client="tun0"), 1, 0))})
    r = world.run(program, [*open_page("plinius-dnsfilter-log"), dump("log")])
    assert log_rows(r.dump("log"))[0]["subtitle"] == f"{OLD_SHOWN} · tun0 · 1 query"


def test_record_types_by_their_names(world, program):
    types = [(2, "NS"), (5, "CNAME"), (6, "SOA"), (12, "PTR"), (15, "MX"), (16, "TXT"), (33, "SRV"),
             (64, "SVCB"), (65, "HTTPS")]
    logged = [entry(10 + i, "t.example", type=t, answer="x") for i, (t, _) in enumerate(types)]
    replies = detail("t.example", logged=logged)
    replies["GetLogNames"] = names_reply((entry(99, "t.example"), 1, 0))
    service(world, Enabled="true", replies=replies)
    r = world.run(program, [*open_page("plinius-dnsfilter-log"),
                            click(type="AdwActionRow", text="t.example"), settle(), dump("name")])
    group = name_page(r.dump("name")).one(type="AdwExpanderRow", text="This phone")
    subs = [x["subtitle"] for x in Screen_of(group).find(type="AdwActionRow") if x["text"] != "This phone"]
    # Newest first
    assert subs == [f"{n} · Answer: x" for _, n in reversed(types)]


def test_check_failing_keeps_what_the_list_said(world, program):
    replies = detail("ads.example")
    replies["Check"] = {"error": "org.plinius.DnsFilter.Error.Failed", "message": "busy"}
    replies["GetLogNames"] = names_reply((entry(9, "ads.example", status=ST_BLOCKED_LIST, reason="L"), 1, 1))
    service(world, Enabled="true", replies=replies)
    r = world.run(program, [*open_page("plinius-dnsfilter-log"),
                            click(type="AdwActionRow", text="ads.example"), settle(), dump("name")])
    head = name_page(r.dump("name")).find(type="AdwActionRow", text="ads.example")[0]
    assert head["subtitle"] == "Blocked by L"
    assert [m["message"] for m in r.warnings()] == ["plinius-dnsfilter: GDBus.Error:"
                                                    "org.plinius.DnsFilter.Error.Failed: busy"]


def test_a_rule_for_one_app_does_not_decide_the_global_button(world, program):
    replies = detail("ads.example", status=ST_BLOCKED_LIST, reason="L")
    replies["GetLogNames"] = names_reply((entry(9, "ads.example", status=ST_BLOCKED_RULE,
                                                reason="||ads.example^$app=app/x"), 1, 1))
    replies["Check"] = {"delay_ms": 500, "reply": "(uint32 4, 'L')"}
    service(world, Enabled="true", replies=replies)
    r = world.run(program, [*open_page("plinius-dnsfilter-log"),
                            click(type="AdwActionRow", text="ads.example"), settle(), dump("before"),
                            wait(700), dump("after")])
    # Until Check answers: as if passed, not "Unblock" of the app's rule
    assert name_page(r.dump("before")).has(type="GtkButton", text="Block Globally")
    assert name_page(r.dump("after")).has(type="GtkButton", text="Unblock")


def test_chart_of_the_last_hours(world, program):
    import time as _time
    hour = int(_time.time()) // 3600 * 3600
    series = (f"[(int64 {hour}, uint32 10, uint32 4, uint32 0, uint32 0), "
              f"(int64 {hour - 3 * 3600}, uint32 7, uint32 0, uint32 0, uint32 0), "
              f"(int64 {hour + 7200}, uint32 99, uint32 0, uint32 0, uint32 0)]")
    service(world, Enabled="true", replies={"GetStats": stats(17, 4, 0, series=series)})
    r = world.run(program, [settle(), push("plinius-dnsfilter"), settle(), dump("main"),
                            push("plinius-dnsfilter-stats"), settle(),
                            select(name="month", type="AdwToggleGroup",
                                   within={"type": "AdwNavigationPage", "tag": "plinius-dnsfilter-stats"}),
                            settle(), dump("stats")])
    # (The bars are drawn, not shown as text: the numbers beside them are)
    assert value_of(r.dump("main"), "Blocked") == "4 (24 %)"
    assert r.dump("stats").has(type="GtkDrawingArea")


def test_more_than_five_hundred_names_keep_the_newest(world, program):
    pages = [names_reply(*[(entry(10000 - p * 150 - i, f"n{p}-{i}.example"), 1, 0) for i in range(150)])
             for p in range(4)]
    service(world, Enabled="true", replies={"GetLogNames": pages})
    ctl = world.control()
    more = click(type="AdwButtonRow", text="Show Older Queries")
    r = world.run(program, [*open_page("plinius-dnsfilter-log"), more, settle(), more, settle(), more,
                            settle(), ctl.emit(PATH, NAME, "Logged", f"({entries(entry(20000, 'live.example'))},)"),
                            settle(), dump("log")], timeout=120)
    rows = log_rows(r.dump("log"))
    assert len(rows) == 500
    assert rows[0]["text"] == "live.example" and rows[-1]["text"] == "n3-48.example"


def test_an_older_answer_for_the_log_is_dropped(world, program):
    service(world, Enabled="true", replies={"GetLogNames": [
        {"delay_ms": 600, "reply": names_reply((entry(1, "stale.example"), 1, 0))},
        names_reply((entry(2, "fresh.example"), 1, 0))]})
    r = world.run(program, [*open_page("plinius-dnsfilter-log"), set_text("fresh", type="GtkSearchEntry"),
                            settle(400), wait(500), dump("log")])
    assert [x["text"] for x in log_rows(r.dump("log"))] == ["fresh.example"]


def test_older_queries_of_a_name(world, program):
    first = [entry(1000 - i, "busy.example") for i in range(150)]
    replies = detail("busy.example", logged=first)
    replies["GetLog"] = [f"({entries(*first)},)", f"({entries(entry(5, 'busy.example'))},)"]
    replies["GetLogNames"] = names_reply((entry(1000, "busy.example"), 151, 0))
    service(world, Enabled="true", replies=replies)
    r = world.run(program, [*open_page("plinius-dnsfilter-log"),
                            click(type="AdwActionRow", text="busy.example"), settle(),
                            click(type="AdwButtonRow", text="Show Older Queries",
                                  within={"type": "AdwNavigationPage", "tag": "plinius-dnsfilter-name"}),
                            settle(), dump("name")], timeout=120)
    assert calls(world, "GetLog") == [("GetLog", [0, 150, "busy.example", 4]),
                                      ("GetLog", [851, 150, "busy.example", 4])]
    group = name_page(r.dump("name")).one(type="AdwExpanderRow", text="This phone")
    assert group["subtitle"] == f"{OLD_SHOWN} · 151 queries"


def test_a_names_page_left_before_its_answers(world, program):
    replies = detail("ads.example", logged=[entry(1, "ads.example")])
    replies["GetLog"] = [{"delay_ms": 500, "reply": f"({entries(entry(1, 'ads.example'))},)"},
                         f"({entries()},)"]
    replies["GetAskersOf"] = [{"delay_ms": 500, "reply": "([('\\tusb\\t10.0.0.2', uint32 1)],)"},
                              "(@a(su) [],)"]
    replies["GetLogNames"] = names_reply((entry(1, "ads.example"), 1, 0),
                                         (entry(2, "b.example"), 1, 0))
    service(world, Enabled="true", replies={**replies, "Check": "(uint32 0, '')"})
    r = world.run(program, [*open_page("plinius-dnsfilter-log"),
                            click(type="AdwActionRow", text="ads.example"), settle(), back(), settle(),
                            click(type="AdwActionRow", text="b.example"), settle(), wait(700),
                            dump("other")])
    # The first name's late answers are not shown on the second name's page
    assert not name_page(r.dump("other")).find(type="AdwExpanderRow")


def test_live_queries_on_a_names_page_from_the_statistics(world, program):
    service(world, Enabled="true", replies={**detail("a.example"), "GetStats": FULL_STATS})
    ctl = world.control()
    r = world.run(program, [*open_page("plinius-dnsfilter-stats"), push("plinius-dnsfilter-stats-asked"),
                            settle(), click(type="AdwActionRow", text="a.example"), settle(),
                            ctl.emit(PATH, NAME, "Logged", f"({entries(entry(3, 'a.example'), entry(4, 'z.example'))},)"),
                            settle(), dump("name")])
    groups = name_page(r.dump("name")).find(type="AdwExpanderRow")
    assert [(g["text"], g["subtitle"]) for g in groups] == [("This phone", f"{OLD_SHOWN} · 1 query")]


def test_back_past_the_log_drops_its_rows(world, program):
    replies = detail("ads.example")
    replies["GetLogNames"] = names_reply((entry(1, "ads.example"), 1, 0))
    service(world, Enabled="true", replies=replies)
    world.run(program, [*open_page("plinius-dnsfilter-log"), click(type="AdwActionRow", text="ads.example"),
                        settle(), pop_to("plinius-dnsfilter"), settle(), push("plinius-dnsfilter-log"),
                        settle()])
    # Shown again from the start: asked anew
    assert len(calls(world, "GetLogNames")) == 2


def test_device_rows_on_a_names_page_are_not_checked_per_app(world, program):
    apps(world)
    replies = detail("ads.example", logged=[entry(2, "ads.example", app="app/org.mozilla.firefox"),
                                            entry(1, "ads.example", kind="usb", client="10.0.0.2")],
                     check_names="([(uint32 0, '')],)")
    replies["GetLogNames"] = names_reply((entry(2, "ads.example"), 2, 0))
    service(world, Enabled="true", replies=replies)
    world.run(program, [*open_page("plinius-dnsfilter-log"), click(type="AdwActionRow", text="ads.example"),
                        settle(), click(type="GtkButton", text="Block Globally"), settle()])
    # Firefox when shown and after the rule; the USB device never
    assert [a for _, a in calls(world, "CheckNames")] == [["app/org.mozilla.firefox", ["ads.example"]]] * 2


@pytest.mark.parametrize("path, member", [
    (["plinius-dnsfilter-log"], "GetLogNames"),
    (["plinius-dnsfilter-stats"], "GetStats"),
    (["plinius-dnsfilter-lists", "plinius-dnsfilter-add-list"], "GetCatalog"),
    (["plinius-dnsfilter-stats", "plinius-dnsfilter-stats-apps", "Firefox"], "GetNamesOf"),
    (["plinius-dnsfilter-log", "a.example"], "GetLog"),
])
def test_open_pages_are_read_again_when_the_service_returns(world, program, path, member):
    apps(world)
    service(world, Enabled="true", replies={
        **detail("a.example"), "GetStats": FULL_STATS, "GetNamesOf": "([('a.example', uint32 1)],)",
        "CheckNames": "([(uint32 0, '')],)", "GetCatalog": CATALOG,
        "GetLogNames": names_reply((entry(1, "a.example"), 1, 0))})
    ctl = world.control()
    steps = [settle(), push("plinius-dnsfilter"), settle()]
    for p in path:
        steps += [push(p) if p.startswith("plinius-") else click(type="AdwActionRow", text=p), settle()]
    world.run(program, [*steps, ctl.release(NAME), settle(), ctl.own(NAME), settle()])
    assert member in [c["member"] for c in world.calls_after("Own", name=NAME)]


def test_who_page_without_a_choice_and_late_answers(world, program):
    apps(world)
    service(world, Enabled="true", replies={
        "GetStats": FULL_STATS,
        "GetNamesOf": [{"delay_ms": 500, "reply": "([('late.example', uint32 1)],)"},
                       "([('b.example', uint32 1)],)"],
        "CheckNames": "([(uint32 0, '')],)"})
    r = world.run(program, [*open_page("plinius-dnsfilter-stats"), push("plinius-dnsfilter-stats-who"),
                            settle(), back(), settle(), push("plinius-dnsfilter-stats-apps"), settle(),
                            click(type="AdwActionRow", text="Firefox"), settle(), back(), settle(),
                            click(type="AdwActionRow", text="Firefox"), settle(), wait(700),
                            dump("who")])
    page = r.dump("who").within(type="AdwNavigationPage", tag="plinius-dnsfilter-stats-who")
    assert [x["text"] for x in page.find(type="AdwActionRow")] == ["b.example"]


def test_who_page_checks_failing_show_nothing(world, program):
    apps(world)
    service(world, Enabled="true", replies={
        "GetStats": FULL_STATS, "GetNamesOf": "([('a.example', uint32 1)],)",
        "CheckNames": {"error": "org.plinius.DnsFilter.Error.Failed", "message": "busy"}})
    r = world.run(program, [*open_page("plinius-dnsfilter-stats"), push("plinius-dnsfilter-stats-apps"),
                            settle(), click(type="AdwActionRow", text="Firefox"), settle(), dump("who")])
    page = r.dump("who").within(type="AdwNavigationPage", tag="plinius-dnsfilter-stats-who")
    assert not page.find(type="AdwActionRow")


@pytest.mark.parametrize("method, tag", [("GetStats", "plinius-dnsfilter-stats"),
                                         ("GetCatalog", "plinius-dnsfilter-add-list")])
def test_failing_answers_leave_pages_empty(world, program, method, tag):
    service(world, Enabled="true", replies={method: {"error": "org.plinius.DnsFilter.Error.Failed",
                                                     "message": "busy"}})
    r = world.run(program, [*open_page(tag), dump("page")])
    page = r.dump("page").within(type="AdwNavigationPage", tag=tag)
    assert not page.find(type="AdwActionRow", subtitle="Ads and tracking")
    assert any(m["message"].endswith("busy") for m in r.warnings())


@pytest.mark.slow
def test_numbers_move_while_a_page_is_shown(world, program):
    service(world, Enabled="true", replies={"GetStats": [stats(1), stats(2)]})
    r = world.run(program, [settle(), push("plinius-dnsfilter"), settle(), wait(10600), settle(),
                            dump("main")], timeout=60)
    assert value_of(r.dump("main"), "Queries") == "2"


def busy_name_page(world, program):
    apps(world)
    pages = []
    for p in range(4):
        part = [entry(10000 - p * 150 - i, "busy.example",
                      app="app/org.mozilla.firefox" if i % 2 else "",
                      status=ST_BLOCKED_LIST if i == 149 else ST_PASSED, reason="L")
                for i in range(150)]
        pages.append(f"({entries(*part)},)")
    replies = detail("busy.example", check_names="([(uint32 0, '')],)")
    replies["GetLog"] = pages
    replies["GetLogNames"] = names_reply((entry(10000, "busy.example"), 600, 4))
    service(world, Enabled="true", replies=replies)
    ctl = world.control()
    more = click(type="AdwButtonRow", text="Show Older Queries",
                 within={"type": "AdwNavigationPage", "tag": "plinius-dnsfilter-name"})
    r = world.run(program, [*open_page("plinius-dnsfilter-log"),
                            click(type="AdwActionRow", text="busy.example"), settle(),
                            more, settle(), more, settle(), more, settle(),
                            ctl.emit(PATH, NAME, "Logged", f"({entries(entry(20000, 'busy.example'))},)"),
                            settle(), dump("name")], timeout=120)
    return name_page(r.dump("name"))


def test_a_live_query_on_a_full_names_page_drops_the_oldest(world, program):
    page = busy_name_page(world, program)
    groups = {g["text"]: g["subtitle"] for g in page.find(type="AdwExpanderRow")}
    # 600 loaded + 1 live: the 101 oldest went (51 of Firefox's with its blocked one, 50 of this
    # phone's), the new one came, 500 are left as in the query log
    assert groups == {"This phone": f"{OLD_SHOWN} · 251 queries",
                      "Firefox": f"{OLD_SHOWN} · 249 queries"}
    # Older ones would leave a gap
    assert not page.has(type="AdwButtonRow", text="Show Older Queries")
