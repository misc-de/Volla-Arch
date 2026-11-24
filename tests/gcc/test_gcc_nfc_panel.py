# The NFC panel (cc-nfc-panel.c): the switch talks to nfcd's settings service
# (org.sailfishos.nfc.Settings, a fake on the test's private system bus, interface as nfcd 1.x has
# it), the contact card group to plinius-cardshare (org.plinius.CardShare on the session bus,
# interface from its Python source) and to the GSettings schema org.plinius.cardshare. The panel
# is listed only when nfcd's bus policy is installed.

import re

import pytest

from conftest import PKG
from gcc_harness import (build_panel, call, click, dump, gcc_session, gcc_world,  # noqa: F401
                         push, set_active, set_text, settle, wait)

NFC = "org.sailfishos.nfc.settings"
NFC_IFACE = "org.sailfishos.nfc.Settings"
# nfcd's org.sailfishos.nfc.Settings (the part the panel uses)
NFC_XML = f"""<node><interface name="{NFC_IFACE}">
 <method name="GetEnabled"><arg name="enabled" type="b" direction="out"/></method>
 <method name="SetEnabled"><arg name="enabled" type="b" direction="in"/></method>
 <signal name="EnabledChanged"><arg name="enabled" type="b"/></signal>
 <signal name="OtherChanged"><arg name="value" type="b"/></signal>
</interface></node>"""
POLICY = "/usr/share/dbus-1/system.d/org.sailfishos.nfc.settings.conf"

CARD = "org.plinius.CardShare"
CARD_PATH = "/org/plinius/CardShare"
CARD_XML = re.search(r'INTERFACE_XML = """(.*?)"""',
                     (PKG / "plinius-cardshare" / "plinius-cardshare.py").read_text(), re.S).group(1)
SCHEMA = "org.plinius.cardshare"

GLUE = r'''
#include "gcc_driver.h"
#include "cc-nfc-panel.h"

CcPanel *
gcc_test_create_panel (GccTest *t)
{
    return g_object_new (CC_TYPE_NFC_PANEL, "window", t->window, NULL);
}

void
gcc_test_setup (GccTest *t)
{
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "static_init")) {
        cc_nfc_panel_static_init_func ();
        json_builder_add_null_value (result);
        return TRUE;
    }
    return FALSE;
}
'''

FIELDS = ["Phone Numbers", "Email Addresses", "Organization and Job Title", "Addresses", "Websites",
          "Birthday", "Notes", "Photo"]
DENIED = {"error": "org.freedesktop.DBus.Error.AccessDenied", "message": "Not privileged"}


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("nfc"), "nfc", ["cc-nfc-panel.c"], GLUE,
                       replacements={"cc-nfc-panel.c": [(f'"{POLICY}"', f'"{{root}}{POLICY}"')]})


def nfcd(world, enabled=True, own=True, **replies):
    replies.setdefault("GetEnabled", f"({'true' if enabled else 'false'},)")
    world.service("system", NFC, "/", NFC_XML, replies=replies, own=own)


def cardshare(world, name="Alice Example", contacts=None, **replies):
    entries = ", ".join(f"({b!r}, {u!r}, {n!r})" for b, u, n in (contacts or []))
    replies.setdefault("ListContacts", f"([{entries}],)" if entries else "(@a(sss) [],)")
    world.service("session", CARD, CARD_PATH, CARD_XML, properties={"CardName": repr(name)},
                  replies=replies)


def nfc_row(screen):
    r = screen.one(type="AdwSwitchRow", text="NFC")
    return r["active"], r.get("sensitive", True)


def gset(key, value):
    return {"op": "gsettings_set", "schema": SCHEMA, "key": key, "value": value}


def gget(key, label=None):
    return {"op": "gsettings_get", "schema": SCHEMA, "key": key, "label": label or key}


def field_rows(screen):
    return {r["text"]: r["active"] for r in screen.find(type="AdwSwitchRow") if r["text"] in FIELDS}


# ----- The switch -----

@pytest.mark.parametrize("enabled", [True, False])
def test_switch_shows_nfcds_state(program, gcc_world, enabled):
    nfcd(gcc_world, enabled=enabled)
    cardshare(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")])
    screen = r.dump("page")
    assert nfc_row(screen) == (enabled, True)
    assert screen.one(type="AdwPreferencesGroup", subtitle=(
        "Reads NFC tags and talks to cards and terminals held to the back of the phone. It only "
        "looks for them while the screen is on."))
    assert len(gcc_world.calls(NFC, "GetEnabled")) == 1
    # Showing switches nothing
    assert not gcc_world.calls(NFC, "SetEnabled")
    assert not r.warnings()


def test_without_nfcd_the_switch_cannot_be_used(program, gcc_world):
    nfcd(gcc_world, own=False)
    cardshare(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")])
    assert nfc_row(r.dump("page")) == (False, False)
    assert not gcc_world.calls(NFC, "GetEnabled")


def test_without_a_system_bus_nfcd_cannot_be_reached(program, gcc_world):
    cardshare(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")], system_bus=False)
    assert nfc_row(r.dump("page")) == (False, False)
    assert [m["message"] for m in r.warnings()
            if m["message"].startswith("Cannot reach nfcd")] != []


def test_unreadable_state_is_a_warning(program, gcc_world):
    nfcd(gcc_world, GetEnabled={"error": "org.sailfishos.nfc.Error.Failed", "message": "broken"})
    cardshare(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")])
    assert nfc_row(r.dump("page")) == (False, True)
    assert [m["message"] for m in r.warnings()] == [
        "Reading the NFC state failed: GDBus.Error:org.sailfishos.nfc.Error.Failed: broken"]


@pytest.mark.parametrize("start, active", [(False, True), (True, False)])
def test_switch_sets_enabled(program, gcc_world, start, active):
    nfcd(gcc_world, enabled=start)
    cardshare(gcc_world)
    r = gcc_world.run(program, [settle(), set_active(active, type="AdwSwitchRow", text="NFC"), settle()])
    assert [c["args"] for c in gcc_world.calls(NFC, "SetEnabled")] == [[active]]
    assert not r.warnings()


def test_refused_switch_says_so_and_shows_the_real_state(program, gcc_world):
    nfcd(gcc_world, enabled=False, SetEnabled=DENIED)
    cardshare(gcc_world)
    r = gcc_world.run(program, [settle(), set_active(True, type="AdwSwitchRow", text="NFC"), settle(),
                                dump("page")])
    screen = r.dump("page")
    assert nfc_row(screen) == (False, True)
    assert screen.has(type="GtkLabel", text="Switching NFC failed")
    # Read again after the refusal
    assert len(gcc_world.calls(NFC, "GetEnabled")) == 2
    assert [m["message"] for m in r.warnings()] == [
        "Switching NFC failed: GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: Not privileged"]


def test_nfcd_changes_show_and_are_not_sent_back(program, gcc_world):
    nfcd(gcc_world, enabled=False)
    cardshare(gcc_world)
    ctl = gcc_world.control("system")
    r = gcc_world.run(program, [settle(), ctl.emit("/", NFC_IFACE, "EnabledChanged", "(true,)"), settle(),
                                dump("on"),
                                # Another signal of the interface says nothing about the switch
                                ctl.emit("/", NFC_IFACE, "OtherChanged", "(false,)"), settle(),
                                dump("other")])
    assert nfc_row(r.dump("on")) == (True, True)
    assert nfc_row(r.dump("other")) == (True, True)
    assert not gcc_world.calls(NFC, "SetEnabled")


def test_nfcd_leaving_and_coming_back(program, gcc_world):
    nfcd(gcc_world, enabled=True)
    cardshare(gcc_world)
    ctl = gcc_world.control("system")
    r = gcc_world.run(program, [settle(), ctl.release(NFC), settle(), dump("gone"),
                                ctl.set_reply("/", NFC_IFACE, "GetEnabled", "(false,)"),
                                ctl.own(NFC), settle(), dump("back")])
    assert nfc_row(r.dump("gone")) == (True, False)
    assert nfc_row(r.dump("back")) == (False, True)
    assert len(gcc_world.calls(NFC, "GetEnabled")) == 2


def test_closing_while_nfcd_answers_is_quiet(program, gcc_world):
    nfcd(gcc_world, GetEnabled={"delay_ms": 300, "reply": "(true,)"},
         SetEnabled={"delay_ms": 300})
    cardshare(gcc_world, ListContacts={"delay_ms": 300, "reply": "(@a(sss) [],)"})
    # The window goes with the answers outstanding: they are cancelled, nothing is touched
    r = gcc_world.run(program, [settle(), push("contact-card-choose"), wait(20)])
    assert not r.warnings()
    r = gcc_world.run(program, [wait(500), set_active(True, type="AdwSwitchRow", text="NFC"), wait(10)])
    assert not r.warnings()


# ----- Listed only with nfcd -----

@pytest.mark.parametrize("installed, visibility", [(True, "visible"), (False, "hidden")])
def test_listed_only_with_nfcd(program, gcc_world, installed, visibility):
    policy = program.root / POLICY.lstrip("/")
    if installed:
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text("<busconfig/>\n")
    elif policy.exists():
        policy.unlink()
    nfcd(gcc_world)
    cardshare(gcc_world)
    r = gcc_world.run(program, [call("static_init")])
    assert r.visibility == {"nfc": visibility}


# ----- The contact card -----

def test_card_row_names_the_contact(program, gcc_world):
    nfcd(gcc_world)
    cardshare(gcc_world, name="Alice Example")
    r = gcc_world.run(program, [settle(), dump("page"), push("contact-card"), settle(), dump("card")])
    page = r.dump("page")
    assert page.one(type="AdwActionRow", text="Contact Card")["subtitle"] == "Alice Example"
    assert page.one(type="AdwPreferencesGroup", subtitle=(
        "Give your contact card to another phone over NFC or a QR code, and take in cards over NFC "
        "or Bluetooth, from the quick setting “My Card”."))
    card = r.dump("card")
    assert card.one(type="AdwActionRow", text="Contact")["subtitle"] == "Alice Example"
    # The schema's default: name, phone, email
    assert field_rows(card) == {f: f in ("Phone Numbers", "Email Addresses") for f in FIELDS}
    assert card.one(type="AdwSwitchRow", text="Photo")["subtitle"] == "Only over NFC, never in the QR code"
    assert card.one(type="AdwSpinRow", text="Seconds to Share or Receive")["value"] == 30
    assert not r.warnings()


def test_no_card_chosen(program, gcc_world):
    nfcd(gcc_world)
    cardshare(gcc_world, name="")
    r = gcc_world.run(program, [settle(), dump("page"), push("contact-card"), settle(), dump("card")])
    assert r.dump("page").one(type="AdwActionRow", text="Contact Card")["subtitle"] == "No card chosen yet"
    assert r.dump("card").one(type="AdwActionRow", text="Contact")["subtitle"] == "None"


def test_without_cardshare_the_name_is_missing(program, gcc_world):
    nfcd(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")])
    assert r.dump("page").one(type="AdwActionRow", text="Contact Card")["subtitle"] == "No card chosen yet"
    assert [m["message"] for m in r.warnings()
            if m["message"].startswith("Reading the contact card failed")]


def test_without_a_session_bus_no_name_is_asked(program, gcc_world):
    nfcd(gcc_world)
    cardshare(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page"), push("contact-card-choose"), settle(),
                                dump("choose")], session_bus=False)
    assert r.dump("page").one(type="AdwActionRow", text="Contact Card").get("subtitle") is None
    assert r.dump("choose").has(type="GtkLabel", text="Loading contacts…")
    assert not gcc_world.calls(CARD)


def test_without_cardshares_schema_no_card_group(program, gcc_world, tmp_path):
    # Schemas of GNOME's desktop only: plinius-cardshare is not installed
    import shutil
    import subprocess
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    for f in gcc_world.session.schemas.glob("org.gnome.desktop.interface.gschema.xml"):
        shutil.copy(f, schemas / f.name)
    (schemas / "dummy.gschema.xml").write_text(
        '<schemalist><schema id="org.plinius.test.dummy" path="/org/plinius/test/dummy/">'
        '<key name="x" type="b"><default>false</default></key></schema></schemalist>')
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True, capture_output=True)
    gcc_world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    nfcd(gcc_world)
    cardshare(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")])
    page = r.dump("page")
    assert not page.has(type="AdwActionRow", text="Contact Card")
    assert nfc_row(page) == (True, True)
    assert not gcc_world.calls(CARD)


def test_fields_follow_the_settings_and_are_written(program, gcc_world):
    nfcd(gcc_world)
    cardshare(gcc_world)
    r = gcc_world.run(program, [settle(), push("contact-card"), settle(),
                                gset("fields", "['name', 'photo', 'birthday']"), settle(), dump("read"),
                                set_active(True, type="AdwSwitchRow", text="Websites"), settle(),
                                gget("fields", "one"),
                                set_active(False, type="AdwSwitchRow", text="Photo"), settle(),
                                gget("fields", "two")])
    assert field_rows(r.dump("read")) == {f: f in ("Photo", "Birthday") for f in FIELDS}
    # In the order of the rows, the name always first; reading did not write
    assert r.value("one") == "['name', 'website', 'birthday', 'photo']"
    assert r.value("two") == "['name', 'website', 'birthday']"


def test_seconds_are_bound(program, gcc_world):
    nfcd(gcc_world)
    cardshare(gcc_world)
    r = gcc_world.run(program, [settle(), push("contact-card"), settle(),
                                {"op": "set_value", "target": {"type": "AdwSpinRow"}, "value": 120},
                                settle(), gget("seconds"), gset("seconds", "uint32 60"), settle(),
                                dump("card")])
    assert r.value("seconds") == "120"
    assert r.dump("card").one(type="AdwSpinRow")["value"] == 60


def test_another_contact_is_read_anew(program, gcc_world):
    nfcd(gcc_world)
    cardshare(gcc_world, name="Alice")
    ctl = gcc_world.control("session")
    r = gcc_world.run(program, [settle(), ctl.set_property(CARD_PATH, CARD, "CardName", "'Bob'"),
                                gset("contact-uid", "'bob'"), settle(), dump("uid"),
                                ctl.set_property(CARD_PATH, CARD, "CardName", "'Carol'"),
                                gset("contact-book", "'work'"), settle(), dump("book"),
                                # Another key of the schema: the name is not asked again
                                ctl.set_property(CARD_PATH, CARD, "CardName", "'Dave'"),
                                gset("seconds", "uint32 40"), gset("fields", "['name']"), settle(),
                                dump("other")])
    assert r.dump("uid").one(type="AdwActionRow", text="Contact Card")["subtitle"] == "Bob"
    assert r.dump("book").one(type="AdwActionRow", text="Contact Card")["subtitle"] == "Carol"
    assert r.dump("other").one(type="AdwActionRow", text="Contact Card")["subtitle"] == "Carol"


CONTACTS = [("book-a", "alice", "Alice Example"), ("book-b", "bob", "Bob <b>Bold</b>"),
            ("book-a", "zoe", "Zoë Zeta")]


def contact_rows(screen):
    group = [g for g in screen.find(type="AdwPreferencesGroup")
             if any(c.get("type") == "AdwActionRow" for c in g.walk())]
    rows = [c for g in group for c in g.walk() if c.get("type") == "AdwActionRow"]
    return [(r["text"], any(c.get("icon") == "object-select-symbolic" for c in r.walk())) for r in rows]


def test_choose_lists_the_contacts_with_the_current_one(program, gcc_world):
    nfcd(gcc_world)
    cardshare(gcc_world, contacts=CONTACTS)
    r = gcc_world.run(program, [gset("contact-uid", "'bob'"), settle(), push("contact-card"), settle(),
                                click(type="AdwActionRow", text="Contact"), settle(), dump("choose")])
    screen = r.dump("choose")
    # Names as they are, no markup
    assert contact_rows(screen) == [("Alice Example", False), ("Bob <b>Bold</b>", True), ("Zoë Zeta", False)]
    assert not screen.has(type="GtkLabel", text="No contacts with a name")
    assert len(gcc_world.calls(CARD, "ListContacts")) == 1


def test_choosing_a_contact_sets_it_and_goes_back(program, gcc_world):
    nfcd(gcc_world)
    cardshare(gcc_world, name="Alice Example", contacts=CONTACTS)
    ctl = gcc_world.control("session")
    r = gcc_world.run(program, [settle(), push("contact-card"), push("contact-card-choose"), settle(),
                                ctl.set_property(CARD_PATH, CARD, "CardName", "'Zoë Zeta'"),
                                click(type="AdwActionRow", text="Zoë Zeta"), settle(),
                                gget("contact-uid", "uid"), gget("contact-book", "book"), dump("after")])
    assert r.value("uid") == "'zoe'"
    assert r.value("book") == "'book-a'"
    after = r.dump("after")
    assert after.has(type="AdwNavigationPage", text="Contact Card")
    # The name is asked again for the new contact
    assert after.one(type="AdwActionRow", text="Contact")["subtitle"] == "Zoë Zeta"


def test_search_filters_by_name_ignoring_case(program, gcc_world):
    nfcd(gcc_world)
    cardshare(gcc_world, contacts=CONTACTS)
    r = gcc_world.run(program, [settle(), push("contact-card-choose"), settle(),
                                set_text("ZOË", type="GtkSearchEntry"), wait(400), settle(), dump("zoe"),
                                set_text("", type="GtkSearchEntry"), wait(400), settle(), dump("all")])
    assert [t for t, _ in contact_rows(r.dump("zoe"))] == ["Zoë Zeta"]
    assert len(contact_rows(r.dump("all"))) == 3


def test_contacts_are_read_anew_each_time(program, gcc_world):
    nfcd(gcc_world)
    cardshare(gcc_world, contacts=CONTACTS[:1])
    ctl = gcc_world.control("session")
    r = gcc_world.run(program, [settle(), push("contact-card"), push("contact-card-choose"), settle(),
                                set_text("ali", type="GtkSearchEntry"), wait(300),
                                {"op": "back"}, settle(),
                                ctl.set_reply(CARD_PATH, CARD, "ListContacts",
                                              "([('b', 'u2', 'Uma'), ('b', 'u3', 'Vic')],)"),
                                push("contact-card-choose"), settle(), dump("again")])
    screen = r.dump("again")
    assert [t for t, _ in contact_rows(screen)] == ["Uma", "Vic"]
    # The search starts empty again
    assert not screen.has(type="GtkSearchEntry", text="ali")
    assert len(gcc_world.calls(CARD, "ListContacts")) == 2


def test_no_contacts_with_a_name(program, gcc_world):
    nfcd(gcc_world)
    cardshare(gcc_world, contacts=[])
    r = gcc_world.run(program, [settle(), push("contact-card-choose"), settle(), dump("choose")])
    assert r.dump("choose").has(type="GtkLabel", text="No contacts with a name")
    assert contact_rows(r.dump("choose")) == []


def test_contacts_that_cannot_be_read_say_why(program, gcc_world):
    nfcd(gcc_world)
    cardshare(gcc_world, ListContacts={"error": "org.plinius.CardShare.Error",
                                       "message": "Address books not available"})
    r = gcc_world.run(program, [settle(), push("contact-card-choose"), settle(), dump("choose")])
    assert r.dump("choose").has(type="GtkLabel", text="Address books not available")
