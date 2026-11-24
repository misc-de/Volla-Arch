# eSIM (cc-esim-dialog.c): the dialog of the Mobile Network panel that manages the profiles of an
# eUICC through lpac, the probe that finds an oFono modem with an eUICC, and the labels of one's
# own for SIM cards (org.plinius.sim). lpac is tests/gcc/fake_lpac.py (answers per call, records
# argv, the LPAC_* environment and the trust store it was given); oFono's manager is a fake on the
# private system bus. The template is tests/gcc/ui/cc-esim-dialog.ui, written by hand from the
# .blp (see test_gcc_blp_ui_match.py).

import base64
import json
import struct
import zlib

import pytest

from gcc_harness import (HERE, UI, apply, build_panel, call, click, compile_resources,  # noqa: F401
                         dump, gcc_session, gcc_world, set_active, set_text, settle, wait,
                         wait_for)

OFONO = "org.ofono"
OFONO_XML = """<node><interface name="org.ofono.Manager">
 <method name="GetModems"><arg type="a(oa{sv})" name="modems" direction="out"/></method>
</interface></node>"""
EUICC = "org.plinius.ofono.EuiccChannel"
SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"
GSMA_CI = "/usr/share/lpac/gsma-ci-bundle.pem"
EID = "89049032123451234512345678901235"
ICCID_A = "8949020000012345678"
ICCID_B = "8949020000087654321"

GLUE = r'''
#include "gcc_driver.h"
#include "cc-esim-dialog.h"

static AdwDialog *dialog;

void
gcc_test_setup (GccTest *t)
{
    if (gcc_test_param (t, "modem")) {
        dialog = cc_esim_dialog_new (gcc_test_param (t, "modem"));
        adw_dialog_present (dialog, GTK_WIDGET (t->window));
    }
}

static void
probe_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    JsonBuilder *b = user_data;
    g_autoptr(GError) error = NULL;
    g_autofree char *path = cc_esim_probe_finish (result, &error);

    json_builder_begin_object (b);
    json_builder_set_member_name (b, "path");
    if (path)
        json_builder_add_string_value (b, path);
    else
        json_builder_add_null_value (b);
    json_builder_set_member_name (b, "error");
    if (error)
        json_builder_add_string_value (b, error->message);
    else
        json_builder_add_null_value (b);
    json_builder_end_object (b);
    g_object_set_data (G_OBJECT (b), "done", GINT_TO_POINTER (1));
}

static void
cancel_soon (GCancellable *cancellable)
{
    g_cancellable_cancel (cancellable);
}

/* The titles of the toasts on screen (AdwToastWidget is libadwaita's own, its label the title) */
static void
collect_toasts (GtkWidget *w, gboolean inside, JsonBuilder *b)
{
    if (!gtk_widget_get_mapped (w))
        return;
    if (!inside && g_str_equal (G_OBJECT_TYPE_NAME (w), "AdwToastWidget"))
        inside = TRUE;
    else if (inside && GTK_IS_LABEL (w) && *gtk_label_get_text (GTK_LABEL (w)))
        json_builder_add_string_value (b, gtk_label_get_text (GTK_LABEL (w)));
    for (GtkWidget *c = gtk_widget_get_first_child (w); c; c = gtk_widget_get_next_sibling (c))
        collect_toasts (c, inside, b);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "wait_toasts")) {
        /* Until a toast shows (at most 10 s), without waiting for the main loop to get quiet: a
         * spinner elsewhere never lets it */
        gint64 end = g_get_monotonic_time () + 10 * G_USEC_PER_SEC;

        for (;;) {
            g_autoptr(JsonBuilder) probe = json_builder_new ();
            g_autoptr(JsonNode) node = NULL;

            json_builder_begin_array (probe);
            collect_toasts (GTK_WIDGET (t->window), FALSE, probe);
            json_builder_end_array (probe);
            node = json_builder_get_root (probe);
            if (json_array_get_length (json_node_get_array (node)) > 0 || g_get_monotonic_time () > end) {
                json_builder_add_value (result, g_steal_pointer (&node));
                return TRUE;
            }
            if (!g_main_context_iteration (NULL, FALSE))
                g_usleep (1000);
        }
    }
    if (g_str_equal (name, "toasts")) {
        json_builder_begin_array (result);
        collect_toasts (GTK_WIDGET (t->window), FALSE, result);
        json_builder_end_array (result);
        return TRUE;
    }
    if (g_str_equal (name, "probe")) {
        /* args: the IMEI (or null), and the ms after which the probe is cancelled (0: never) */
        g_autoptr(GCancellable) cancellable = g_cancellable_new ();
        gint64 cancel_ms = json_array_get_length (args) > 1 ? json_array_get_int_element (args, 1) : 0;
        gint64 end = g_get_monotonic_time () + 20 * G_USEC_PER_SEC;

        if (cancel_ms < 0)
            g_cancellable_cancel (cancellable);
        else if (cancel_ms > 0)
            g_timeout_add_once (cancel_ms, (GSourceOnceFunc) cancel_soon, cancellable);
        cc_esim_probe_async (json_array_get_string_element (args, 0), cancellable, probe_done_cb, result);
        while (!g_object_get_data (G_OBJECT (result), "done") && g_get_monotonic_time () < end)
            if (!g_main_context_iteration (NULL, FALSE))
                g_usleep (1000);
        if (!g_object_get_data (G_OBJECT (result), "done"))
            json_builder_add_string_value (result, "timeout");
        g_object_set_data (G_OBJECT (result), "done", NULL);
        return TRUE;
    }
    if (g_str_equal (name, "label")) {
        g_autofree char *label = cc_sim_label_dup (json_array_get_string_element (args, 0));

        if (label)
            json_builder_add_string_value (result, label);
        else
            json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "label_set")) {
        cc_sim_label_set (json_array_get_string_element (args, 0), json_array_get_string_element (args, 1));
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "labels")) {
        GSettings *settings = cc_sim_label_settings ();
        g_autoptr(GVariant) labels = settings ? g_settings_get_value (settings, "labels") : NULL;
        g_autofree char *text = labels ? g_variant_print (labels, FALSE) : NULL;

        if (text)
            json_builder_add_string_value (result, text);
        else
            json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "clipboard")) {
        GdkClipboard *clipboard = gtk_widget_get_clipboard (GTK_WIDGET (t->window));
        GdkContentProvider *content = gdk_clipboard_get_content (clipboard);
        g_auto(GValue) value = G_VALUE_INIT;

        g_value_init (&value, G_TYPE_STRING);
        if (content && gdk_content_provider_get_value (content, &value, NULL))
            json_builder_add_string_value (result, g_value_get_string (&value));
        else
            json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "open")) {
        dialog = cc_esim_dialog_new (json_array_get_string_element (args, 0));
        adw_dialog_present (dialog, GTK_WIDGET (t->window));
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "close")) {
        adw_dialog_force_close (dialog);
        json_builder_add_null_value (result);
        return TRUE;
    }
    return FALSE;
}
'''


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    out = tmp_path_factory.mktemp("esim")
    resources = compile_resources(out, UI / "esim.gresource.xml", "cc_test_esim")
    reps = [(f'"{SYSTEM_CA}"', f'"{{root}}{SYSTEM_CA}"'), (f'"{GSMA_CI}"', f'"{{root}}{GSMA_CI}"')]
    return build_panel(gcc_session, out, "esim", ["cc-esim-dialog.c"], GLUE,
                       replacements={"cc-esim-dialog.c": reps}, sources=[resources])


@pytest.fixture
def root(program):
    """The program's root with both certificate bundles."""
    import shutil
    for d in list(program.root.iterdir()):
        shutil.rmtree(d) if d.is_dir() else d.unlink()
    for path, text in ((SYSTEM_CA, "SYSTEM CAS\n"), (GSMA_CI, "GSMA CI ROOTS\n")):
        f = program.root / path.lstrip("/")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return program.root


# ----- lpac's answers -----

def progress(step):
    return {"type": "progress", "payload": {"code": 0, "message": step, "data": None}}


def ok(data=None, *, sleep=0, err=""):
    a = {"out": [progress("es10b_get_euicc_challenge_and_info"),
                 {"type": "lpa", "payload": {"code": 0, "message": "success", "data": data}}]}
    if sleep:
        a["sleep"] = sleep
    if err:
        a["err"] = err
    return a


def fail(message, data=None, *, err="", code=-1):
    payload = {"code": code, "data": data}
    if message is not None:
        payload["message"] = message
    return {"out": [{"type": "lpa", "payload": payload}], "err": err, "exit": 255}


def png():
    """A 1×1 PNG."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    raw = b"\x00\xff\x00\x00\xff"
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
                            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")).decode()


CHIP = {"eidValue": EID, "EUICCInfo2": {"profileVersion": "2.3.1",
                                        "extCardResource": {"freeNonVolatileMemory": 123456}}}


def profile(iccid, state="disabled", nickname=None, provider=None, name=None, klass="operational",
            icon=None):
    return {"iccid": iccid, "isdpAid": "A0000005591010FFFFFFFF8900001100", "profileState": state,
            "profileNickname": nickname, "serviceProviderName": provider, "profileName": name,
            "iconType": "png" if icon else None, "icon": icon, "profileClass": klass}


TWO = [profile(ICCID_A, "enabled", provider="Telco", name="Telco Prepaid", icon=png()),
       profile(ICCID_B, nickname="Travel", provider="Roam", name="Roam Data", klass="test")]


class Lpac:
    def __init__(self, world):
        self.world = world
        self.answers = {"chip info": [ok(CHIP)], "profile list": [ok(TWO)]}
        self.log = world.tmp / "lpac-calls.jsonl"
        scenario = world.tmp / "lpac.json"
        self.scenario = scenario
        world.fake_bin.stub("lpac", f'exec python3 -I {HERE / "fake_lpac.py"} {scenario} "$@"')

    def __setitem__(self, key, answers):
        self.answers[key] = answers if isinstance(answers, list) else [answers]
        self.write()

    def write(self):
        self.scenario.write_text(json.dumps({"log": str(self.log), "state": str(self.world.tmp / "lpac.state"),
                                             "answers": self.answers}))

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def argvs(self):
        return [" ".join(c["argv"]) for c in self.calls()]


@pytest.fixture
def lpac(gcc_world, root):
    lp = Lpac(gcc_world)
    lp.write()
    return lp


def run(world, steps, modem="/ril_0", **kw):
    return world.run(world.program, steps, params={"modem": modem} if modem else {}, **kw)


@pytest.fixture
def world(gcc_world, program):
    gcc_world.program = program
    return gcc_world


CONTENT = dict(type="AdwActionRow", text="EID")


def loaded():
    return wait_for(**CONTENT)


def profile_rows(screen):
    group = screen.one(type="AdwPreferencesGroup", text="Profiles")
    out = []
    for row in group.get("children", []):
        if row["type"] != "AdwActionRow":
            continue
        icons = [c.get("icon") for c in row.get("children", []) if c["type"] == "GtkImage"]
        out.append((row["text"], row.get("subtitle"), icons))
    return out


def subtitle(screen, title):
    return screen.one(type="AdwActionRow", text=title).get("subtitle")


# ----- Loading -----

def test_chip_and_profiles_are_shown(world, lpac):
    r = run(world, [loaded(), dump("main"), call("labels")])
    s = r.dump("main")
    assert s.one(type="CcEsimDialog")["text"] == "eSIM"
    assert subtitle(s, "EID") == EID
    assert subtitle(s, "Specification") == "2.3.1"
    assert subtitle(s, "Free Memory") == "123.5\u00a0kB"
    assert s.one(type="AdwPreferencesGroup", text="Profiles")["subtitle"] == \
        "Only one profile can be active at a time."
    assert profile_rows(s) == [
        # The operator's icon (no icon name), the tick of the active one
        ("Telco Prepaid", "Active · Telco", ["object-select-symbolic"]),
        ("Travel", "Roam · Test profile", ["auth-sim-symbolic"]),
    ]
    assert lpac.argvs() == ["chip info", "profile list"]
    # The nickname on the eUICC became the card's label
    assert r.value("labels") == f"{{'{ICCID_B}': 'Travel'}}"
    assert not r.warnings()


def test_lpac_gets_the_modem_and_a_trust_store(world, lpac):
    world.extra_env["LPAC_HTTP_TRUST_STORE"] = "/somewhere/else.pem"
    run(world, [loaded()], modem="/ril_1")
    for c in lpac.calls():
        assert c["env"] == {"LPAC_APDU": "ofono", "LPAC_APDU_OFONO_MODEM": "/ril_1",
                            "LPAC_HTTP_SSL_VERIFY": "1",
                            "LPAC_HTTP_TRUST_STORE": str(world.runtime / "plinius-lpac-trust.pem")}
        # The system's CAs and the GSMA CI roots together
        assert c["trust"] == "SYSTEM CAS\n\nGSMA CI ROOTS\n"
        assert c["lc_all"] == "C.UTF-8"


def test_without_the_gsma_roots_the_systems_store_is_used(world, lpac, root):
    (root / GSMA_CI.lstrip("/")).unlink()
    world.extra_env["LPAC_HTTP_TRUST_STORE"] = "/somewhere/else.pem"
    run(world, [loaded()])
    assert all("LPAC_HTTP_TRUST_STORE" not in c["env"] and c["env"]["LPAC_HTTP_SSL_VERIFY"] == "1"
               for c in lpac.calls())


def test_without_system_cas_only_the_gsma_roots(world, lpac, root):
    (root / SYSTEM_CA.lstrip("/")).unlink()
    run(world, [loaded()])
    assert {c["trust"] for c in lpac.calls()} == {"\nGSMA CI ROOTS\n"}


def test_unwritable_trust_store_warns_and_goes_on(world, lpac):
    (world.runtime / "plinius-lpac-trust.pem").mkdir()
    r = run(world, [loaded()])
    assert all("LPAC_HTTP_TRUST_STORE" not in c["env"] for c in lpac.calls())
    msgs = [m["message"] for m in r.warnings()]
    # Once for each call
    assert len(msgs) == 2 and all(m.startswith(
        f"Could not write {world.runtime}/plinius-lpac-trust.pem: ") for m in msgs)


def test_while_loading_a_spinner(world, lpac):
    lpac["chip info"] = ok(CHIP, sleep=0.6)
    r = run(world, [call("open", "/ril_0"), wait(150), dump("loading"), loaded()], modem=None)
    s = r.dump("loading")
    assert s.has(type="AdwSpinner")
    assert not s.has(**CONTENT)


@pytest.mark.parametrize("answer, shown", [
    (fail("es10c_get_eid", "SCARD_E_NO_SMARTCARD"), "es10c_get_eid: SCARD_E_NO_SMARTCARD"),
    (fail("es10c_get_eid", {"why": "object"}), "es10c_get_eid"),
    (fail("es10c_get_eid", ""), "es10c_get_eid"),
    (fail(None), "?"),
    ({"out": ["not json", "[1, 2]", json.dumps({"type": "progress", "payload": {}}), ""]},
     "no result"),
    ({"out": [], "exit": 1}, "no result"),
])
def test_chip_that_cannot_be_read_shows_why(world, lpac, answer, shown):
    lpac["chip info"] = answer
    r = run(world, [wait_for(type="AdwStatusPage", text="eSIM Not Available"), dump("error")])
    page = r.dump("error").one(type="AdwStatusPage")
    assert page["subtitle"] == shown
    assert lpac.argvs() == ["chip info"]
    assert [m["message"] for m in r.warnings()] == [f"Reading the eSIM failed: {shown}"]


def test_result_without_payload_is_no_result_without_criticals(world, lpac):
    lpac["chip info"] = {"out": [json.dumps({"type": "lpa"})]}
    r = run(world, [wait_for(type="AdwStatusPage", text="eSIM Not Available"), dump("error")])
    assert r.dump("error").one(type="AdwStatusPage")["subtitle"] == "no result"
    assert [m["message"] for m in r.warnings()] == ["Reading the eSIM failed: no result"]


@pytest.mark.parametrize("chip", [{"eidValue": EID}, {"eidValue": EID, "EUICCInfo2": {}}])
def test_chip_without_euiccinfo2_gives_no_criticals(world, lpac, chip):
    lpac["chip info"] = ok(chip)
    r = run(world, [loaded(), dump("main")])
    assert subtitle(r.dump("main"), "Free Memory") is None
    assert not r.warnings()


def test_the_last_result_line_counts(world, lpac):
    lpac["chip info"] = {"out": [{"type": "lpa", "payload": {"code": 0, "data": CHIP}},
                                 {"type": "lpa", "payload": {"code": 5, "message": "later"}}]}
    r = run(world, [wait_for(type="AdwStatusPage", text="eSIM Not Available"), dump("error")])
    assert r.dump("error").one(type="AdwStatusPage")["subtitle"] == "later"


def test_chip_answer_without_an_object_shows_the_error_page(world, lpac):
    lpac["chip info"] = ok(None)
    r = run(world, [wait_for(type="AdwStatusPage", text="eSIM Not Available"), dump("error")])
    assert "subtitle" not in r.dump("error").one(type="AdwStatusPage")
    assert [m["message"] for m in r.warnings()] == ["Reading the eSIM failed: (null)"]


def test_lpac_that_cannot_start_says_why(world, lpac):
    world.fake_bin.path.joinpath("lpac").write_text("#!/nonexistent/interpreter\n")
    r = run(world, [wait_for(type="AdwStatusPage", text="eSIM Not Available"), dump("error")])
    assert r.dump("error").one(type="AdwStatusPage")["subtitle"] == \
        "Failed to execute child process “lpac” (No such file or directory)"


def test_unreadable_lpac_output_says_why(world, lpac):
    lpac["chip info"] = {"out": ["\udcff\udcfe"]}
    r = run(world, [wait_for(type="AdwStatusPage", text="eSIM Not Available"), dump("error")])
    assert "UTF-8" in r.dump("error").one(type="AdwStatusPage")["subtitle"]


def test_try_again_reads_the_chip_again(world, lpac):
    lpac["chip info"] = [fail("es10c_get_eid", "busy"), ok(CHIP)]
    r = run(world, [wait_for(type="AdwStatusPage", text="eSIM Not Available"),
                    click(type="GtkButton", text="Try Again"), loaded(), dump("main")])
    assert lpac.argvs() == ["chip info", "chip info", "profile list"]
    assert subtitle(r.dump("main"), "EID") == EID


@pytest.mark.parametrize("chip, version, memory", [
    ({"eidValue": EID}, None, None),
    ({"eidValue": EID, "EUICCInfo2": {"profileVersion": "2.2.0"}}, "2.2.0", None),
    ({"EUICCInfo2": {"extCardResource": {}}}, None, "0 bytes"),
])
def test_chip_facts_that_are_missing_stay_empty(world, lpac, chip, version, memory):
    lpac["chip info"] = ok(chip)
    r = run(world, [loaded(), dump("main")])
    s = r.dump("main")
    assert subtitle(s, "Specification") == version
    assert subtitle(s, "Free Memory") == memory
    assert subtitle(s, "EID") == (EID if "eidValue" in chip else None)


def test_no_profiles_yet(world, lpac):
    lpac["profile list"] = ok([])
    r = run(world, [loaded(), dump("main")])
    s = r.dump("main")
    assert profile_rows(s) == []
    assert s.one(type="AdwPreferencesGroup", text="Profiles")["subtitle"] == \
        "There are no profiles on this eSIM yet."


def test_profile_titles_and_icons(world, lpac):
    lpac["profile list"] = ok([
        profile("8949000000000000001", icon="not base64 of an image"),
        profile("8949000000000000002", nickname="", name="", provider=""),
        {"iccid": "8949000000000000003", "profileState": "enabled", "profileName": None},
    ])
    r = run(world, [loaded(), dump("main")])
    assert profile_rows(r.dump("main")) == [
        ("8949000000000000001", None, ["auth-sim-symbolic"]),
        ("8949000000000000002", None, ["auth-sim-symbolic"]),
        ("8949000000000000003", "Active", ["auth-sim-symbolic", "object-select-symbolic"]),
    ]


def test_profile_list_not_an_array_shows_no_rows(world, lpac):
    lpac["profile list"] = ok({"not": "a list"})
    r = run(world, [loaded(), dump("main")])
    assert profile_rows(r.dump("main")) == []


def test_profiles_that_cannot_be_read_leave_a_way_to_try_again(world, lpac):
    lpac["profile list"] = [fail("es10c_get_profiles_info", "busy"), ok(TWO)]
    r = run(world, [call("open", "/ril_0"), call("wait_toasts"), settle(), dump("after"),
                    click(type="GtkButton", text="Try Again"), loaded(), dump("again")], modem=None)
    page = r.dump("after").one(type="AdwStatusPage")
    assert (page["text"], page["subtitle"]) == ("eSIM Not Available", "es10c_get_profiles_info: busy")
    # Trying again reads the chip and the profiles again
    assert lpac.argvs() == ["chip info", "profile list", "chip info", "profile list"]
    assert r.dump("again").has(**CONTENT)


def test_profiles_that_cannot_be_read_say_so(world, lpac):
    lpac["profile list"] = fail("es10c_get_profiles_info", "busy")
    # The spinner stays: no settling (it never gets quiet), only time for lpac
    r = run(world, [call("open", "/ril_0"), call("wait_toasts"), dump("after"), call("toasts")],
            modem=None)
    assert r.value("wait_toasts") == [
        "Reading the profiles failed (es10c_get_profiles_info: busy)"]
    assert [m["message"] for m in r.warnings()] == [
        "Reading the profiles failed: es10c_get_profiles_info: busy"]


def test_copy_eid(world, lpac):
    r = run(world, [loaded(), click(type="GtkButton", tooltip="Copy EID"),
                    call("wait_toasts"), dump("after"), call("toasts"), call("clipboard")])
    assert r.value("clipboard") == EID
    assert r.value("wait_toasts") == ["EID copied"]


# ----- A profile's page -----

def open_profile(title):
    return [loaded(), click(type="AdwActionRow", text=title), wait_for(tag="profile")]


def page_facts(screen):
    page = screen.one(tag="profile")
    p = Screen_of(page)
    return {"title": page["text"],
            "label": p.one(type="AdwEntryRow", text="Label")["value"],
            "provider": p.one(type="AdwActionRow", text="Provider").get("subtitle"),
            "name": p.one(type="AdwActionRow", text="Profile Name").get("subtitle"),
            "iccid": p.one(type="AdwActionRow", text="ICCID").get("subtitle"),
            "type": p.one(type="AdwActionRow", text="Type").get("subtitle"),
            "active": p.one(type="AdwSwitchRow", text="Active")["active"],
            "deletable": p.one(type="AdwButtonRow", text="Delete Profile…").get("sensitive", True)}


def Screen_of(node):
    from gcc_harness import Screen
    return Screen([node])


def test_page_of_the_active_profile(world, lpac):
    r = run(world, [*open_profile("Telco Prepaid"), dump("page")])
    assert page_facts(r.dump("page")) == {
        "title": "Telco Prepaid", "label": "", "provider": "Telco", "name": "Telco Prepaid",
        "iccid": ICCID_A, "type": "Operator profile", "active": True,
        # SGP.22: an enabled profile cannot be deleted
        "deletable": False}
    # Showing it changes nothing
    assert lpac.argvs() == ["chip info", "profile list"]


@pytest.mark.parametrize("klass, shown", [("test", "Test profile"),
                                          ("provisioning", "Provisioning profile"),
                                          (None, "Operator profile")])
def test_page_of_another_profile(world, lpac, klass, shown):
    lpac["profile list"] = ok([profile(ICCID_B, nickname="Travel", provider="Roam",
                                       name="Roam Data", klass=klass)])
    r = run(world, [*open_profile("Travel"), dump("page")])
    assert page_facts(r.dump("page")) == {
        "title": "Travel", "label": "Travel", "provider": "Roam", "name": "Roam Data",
        "iccid": ICCID_B, "type": shown, "active": False, "deletable": True}


def test_a_row_of_a_profile_gone_meanwhile_does_nothing(world, lpac):
    # The list is read again after the switch: the profile whose row is clicked is gone
    lpac["profile list"] = [ok(TWO), ok([TWO[0]])]
    lpac["profile enable"] = ok()
    r = run(world, [*open_profile("Travel"), set_active(True, type="AdwSwitchRow", text="Active"),
                    call("wait_toasts"), settle(200), dump("after"), call("toasts")])
    # Still on the page of the profile, nothing more to see of it; Delete finds nothing to delete
    assert lpac.argvs()[:4] == ["chip info", "profile list", f"profile enable {ICCID_B} 1",
                                "profile list"]


def test_a_profile_without_iccid_has_no_page(world, lpac):
    lpac["profile list"] = ok([{"profileState": "disabled", "profileName": "Nameless"}])
    r = run(world, [loaded(), click(type="AdwActionRow", text="Nameless"), settle(100),
                    dump("after")])
    # Nothing to show or change without the card's number
    assert not r.dump("after").has(tag="profile")
    assert lpac.argvs() == ["chip info", "profile list"]


def test_label_of_ones_own_and_an_empty_one(world, lpac):
    lpac["profile list"] = ok([profile(ICCID_A, nickname="Old", name="Telco Prepaid"),
                               profile(ICCID_B, nickname="Travel", name="Roam Data")])
    r = run(world, [call("label_set", ICCID_A, "Work"), call("label_set", ICCID_B, ""),
                    {"op": "setup"}, loaded(), dump("main")])
    # One's own label wins over the nickname; a removed one leaves the profile's name
    assert [t for t, _s, _i in profile_rows(r.dump("main"))] == ["Work", "Roam Data"]


def test_switching_a_profile_on(world, lpac):
    lpac["profile list"] = [ok(TWO), ok([profile(ICCID_A, provider="Telco", name="Telco Prepaid"),
                                         profile(ICCID_B, "enabled", nickname="Travel",
                                                 provider="Roam", name="Roam Data")])]
    lpac["profile enable"] = ok(sleep=0.5)
    r = run(world, [*open_profile("Travel"), set_active(True, type="AdwSwitchRow", text="Active"),
                    wait(150), dump("busy"),
                    call("wait_toasts"), settle(300), dump("after"), call("toasts")])
    busy = r.dump("busy")
    # One call at a time: the pages wait, the spinner turns
    assert busy.one(type="AdwNavigationView").get("sensitive") is False
    assert lpac.argvs() == ["chip info", "profile list", f"profile enable {ICCID_B} 1",
                            "profile list", "notification process -a -r"]
    s = r.dump("after")
    assert r.value("wait_toasts") == ["Done. The network may take a moment."]
    # The page shows the new state
    assert page_facts(s)["active"] is True and page_facts(s)["deletable"] is False


def test_switching_a_profile_off(world, lpac):
    lpac["profile disable"] = ok()
    run(world, [*open_profile("Telco Prepaid"),
                set_active(False, type="AdwSwitchRow", text="Active"),
                call("wait_toasts"), settle(300)])
    assert lpac.argvs()[2] == f"profile disable {ICCID_A} 1"


def test_failed_switch_shows_the_real_state_again(world, lpac):
    lpac["profile enable"] = fail("es10c_enable_profile", "profile not in disabled state")
    lpac["notification process"] = fail("es9p_handle_notification", "no internet")
    r = run(world, [*open_profile("Travel"), set_active(True, type="AdwSwitchRow", text="Active"),
                    call("wait_toasts"), settle(300), dump("after"), call("toasts")])
    s = r.dump("after")
    assert r.value("wait_toasts") == [
        "Switching the profile failed (es10c_enable_profile: profile not in disabled state)"]
    assert page_facts(s)["active"] is False
    assert lpac.argvs()[3:] == ["profile list", "notification process -a -r"]
    # The notifications that could not go out are no matter for a warning (GTK's own about the
    # focus of the label's entry left aside)
    assert [m["message"] for m in r.warnings(ignore=[r"^GtkText - did not receive a focus-out"])] == [
        "Switching the profile failed: es10c_enable_profile: profile not in disabled state"]


def test_label_goes_to_the_settings_and_the_euicc(world, lpac):
    r = run(world, [*open_profile("Telco Prepaid"), set_text("  Work  ", type="AdwEntryRow", text="Label"),
                    apply(type="AdwEntryRow", text="Label"), settle(300), call("labels"),
                    call("label", ICCID_A + "F")])
    assert lpac.argvs()[2:] == [f"profile nickname {ICCID_A} Work", "profile list"]
    assert r.value("labels") == f"{{'{ICCID_B}': 'Travel', '{ICCID_A}': 'Work'}}"
    assert r.value("label") == "Work"


def test_an_empty_label_removes_the_nickname(world, lpac):
    r = run(world, [*open_profile("Travel"), set_text(" ", type="AdwEntryRow", text="Label"),
                    apply(type="AdwEntryRow", text="Label"), settle(300), call("labels"),
                    dump("after"), call("toasts")])
    assert lpac.argvs()[2:] == [f"profile nickname {ICCID_B}", "profile list"]
    # Kept as an empty entry: the nickname is not taken over again
    assert r.value("labels") == f"{{'{ICCID_B}': ''}}"
    assert page_facts(r.dump("after"))["title"] == "Roam Data"


def test_failed_rename_keeps_the_label(world, lpac):
    lpac["profile nickname"] = fail("es10c_set_nickname", "busy")
    r = run(world, [*open_profile("Telco Prepaid"), set_text("Work", type="AdwEntryRow", text="Label"),
                    apply(type="AdwEntryRow", text="Label"), call("wait_toasts"),
                    settle(300), dump("after"), call("toasts"), call("label", ICCID_A)])
    assert r.value("wait_toasts") == ["Renaming the profile failed (es10c_set_nickname: busy)"]
    assert r.value("label") == "Work"
    assert lpac.argvs()[-1] == "profile list"


def test_delete_asks_first(world, lpac):
    r = run(world, [*open_profile("Travel"), click(type="AdwButtonRow", text="Delete Profile…"),
                    wait_for(type="AdwAlertDialog"), dump("asked"),
                    click(type="GtkButton", text="Cancel"), settle(200), dump("after"), call("toasts")])
    alert = r.dump("asked").one(type="AdwAlertDialog")
    assert alert["text"] == "Delete Profile?"
    assert alert["subtitle"] == ("“Travel” will be removed from the eSIM for good. To use it again, "
                                 "you need a new activation code from the provider.")
    assert lpac.argvs() == ["chip info", "profile list"]
    assert r.dump("after").has(tag="profile")


def test_delete_removes_the_profile_and_its_label(world, lpac):
    lpac["profile list"] = [ok(TWO), ok([TWO[0]])]
    lpac["profile delete"] = ok()
    r = run(world, [*open_profile("Travel"), click(type="AdwButtonRow", text="Delete Profile…"),
                    wait_for(type="AdwAlertDialog"), click(type="GtkButton", text="Delete"),
                    call("wait_toasts"), settle(300), dump("after"), call("toasts"), call("labels")])
    s = r.dump("after")
    assert lpac.argvs()[2:] == [f"profile delete {ICCID_B}", "profile list",
                                "notification process -a -r"]
    assert r.value("wait_toasts") == ["Profile deleted"]
    assert not s.has(tag="profile") and s.has(**CONTENT)
    assert [t for t, _s, _i in profile_rows(s)] == ["Telco Prepaid"]
    assert r.value("labels") == "{}"


def test_failed_delete_stays_on_the_page(world, lpac):
    lpac["profile delete"] = fail("es10c_delete_profile", "busy")
    r = run(world, [*open_profile("Travel"), click(type="AdwButtonRow", text="Delete Profile…"),
                    wait_for(type="AdwAlertDialog"), click(type="GtkButton", text="Delete"),
                    call("wait_toasts"), settle(300), dump("after"), call("toasts"), call("labels")])
    s = r.dump("after")
    assert r.value("wait_toasts") == ["Deleting the profile failed (es10c_delete_profile: busy)"]
    assert s.has(tag="profile")
    assert r.value("labels") == f"{{'{ICCID_B}': 'Travel'}}"


def test_delete_of_a_profile_gone_meanwhile_does_nothing(world, lpac):
    lpac["profile list"] = [ok(TWO), ok([TWO[0]])]
    lpac["profile enable"] = fail("es10c_enable_profile", "gone")
    r = run(world, [*open_profile("Travel"), set_active(True, type="AdwSwitchRow", text="Active"),
                    call("wait_toasts"), settle(300),
                    click(type="AdwButtonRow", text="Delete Profile…"), settle(100), dump("after"), call("toasts")])
    assert not r.dump("after").has(type="AdwAlertDialog")
    assert "profile delete" not in " ".join(lpac.argvs())


# ----- Adding a profile -----

def open_add():
    return [loaded(), click(type="AdwButtonRow", text="Add Profile…"), wait_for(tag="add")]


def download_sensitive(screen):
    return screen.one(type="AdwButtonRow", text="Download Profile").get("sensitive", True)


@pytest.mark.parametrize("code, sensitive", [
    ("LPA:1$smdp.example.com$MATCH", True), ("  lpa:1$smdp.example.com$X", True),
    ("LPA:1$", False), ("http://smdp.example.com", False), ("", False),
])
def test_download_needs_an_activation_code(world, lpac, code, sensitive):
    r = run(world, [*open_add(), dump("empty"), set_text(code, type="AdwEntryRow", text="Activation Code"),
                    dump("typed")])
    assert download_sensitive(r.dump("empty")) is False
    assert download_sensitive(r.dump("typed")) is sensitive


@pytest.mark.parametrize("confirmation, args", [
    ("", "-a LPA:1$smdp.example.com$MATCH"),
    ("1234", "-a LPA:1$smdp.example.com$MATCH -c 1234"),
])
def test_download_adds_the_profile(world, lpac, confirmation, args):
    lpac["profile download"] = ok({"iccid": ICCID_B}, sleep=0.5)
    r = run(world, [*open_add(), set_text(" LPA:1$smdp.example.com$MATCH ", type="AdwEntryRow",
                                          text="Activation Code"),
                    set_text(confirmation, type="AdwEntryRow", text="Confirmation Code (if required)"),
                    click(type="AdwButtonRow", text="Download Profile"), wait(150), dump("running"),
                    call("wait_toasts"), settle(300), dump("after"), call("toasts")])
    assert r.dump("running").has(type="GtkLabel", text="Downloading the profile. This can take a minute.")
    assert lpac.argvs()[2:] == [f"profile download {args}", "profile list",
                                "notification process -a -r"]
    s = r.dump("after")
    assert r.value("wait_toasts") == ["Profile added. Switch it on to use it."]
    assert not s.has(tag="add") and s.has(**CONTENT)


def test_failed_download_stays_on_the_page(world, lpac):
    lpac["profile download"] = fail("es9p_authenticate_client", "Matching ID refused",
                                    err="curl_easy_perform() failed: Couldn't connect to server")
    r = run(world, [*open_add(), set_text("LPA:1$smdp.example.com$BAD", type="AdwEntryRow",
                                          text="Activation Code"),
                    click(type="AdwButtonRow", text="Download Profile"),
                    call("wait_toasts"), settle(200), dump("after"), call("toasts")])
    s = r.dump("after")
    assert r.value("wait_toasts") == [
        "Downloading the profile failed (es9p_authenticate_client: Matching ID refused)"]
    assert s.has(tag="add")
    assert not s.has(type="GtkLabel", text="Downloading the profile. This can take a minute.")
    # A refused connection is no matter of the certificate: nothing is asked
    assert not s.has(type="AdwAlertDialog")
    assert lpac.argvs()[2:] == ["profile download -a LPA:1$smdp.example.com$BAD"]


def test_add_page_starts_empty_again(world, lpac):
    lpac["profile download"] = fail("es9p", "no")
    r = run(world, [*open_add(), set_text("LPA:1$a$b", type="AdwEntryRow", text="Activation Code"),
                    set_text("99", type="AdwEntryRow", text="Confirmation Code (if required)"),
                    click(type="AdwButtonRow", text="Download Profile"),
                    call("wait_toasts"),
                    {"op": "action", "name": "navigation.pop",
                     "target": {"type": "AdwEntryRow", "text": "Activation Code"}},
                    wait_for(tag="add", gone=True),
                    click(type="AdwButtonRow", text="Add Profile…"), wait_for(tag="add"),
                    dump("again")])
    s = r.dump("again")
    assert s.one(type="AdwEntryRow", text="Activation Code")["value"] == ""
    assert s.one(type="AdwEntryRow", text="Confirmation Code (if required)")["value"] == ""
    assert download_sensitive(s) is False


TLS_ERRORS = [
    "curl_easy_perform() failed: SSL certificate OpenSSL verify result: unable to get local "
    "issuer certificate (20)",
    "curl_easy_perform() failed: SSL peer certificate or SSH remote key was not OK",
    "curl_easy_perform() failed: SSL: no alternative certificate subject name matches target "
    "hostname 'smdp.example.com'",
]


def unverified_download(err):
    return fail("es9p_initiate_authentication", None, err=err)


@pytest.mark.parametrize("err", TLS_ERRORS)
def test_unverified_server_continues_only_when_asked(world, lpac, err):
    lpac["profile download"] = [unverified_download(err), ok({"iccid": ICCID_B})]
    r = run(world, [*open_add(), set_text("LPA:1$smdp.example.com$M", type="AdwEntryRow",
                                          text="Activation Code"),
                    click(type="AdwButtonRow", text="Download Profile"),
                    wait_for(type="AdwAlertDialog"), dump("asked"),
                    click(type="GtkButton", text="Continue Unverified"),
                    call("wait_toasts"), settle(300), dump("after"), call("toasts")])
    alert = r.dump("asked").one(type="AdwAlertDialog")
    assert alert["text"] == "Server Not Verified"
    assert alert["subtitle"].startswith("The identity of the eSIM server could not be confirmed.")
    calls = lpac.calls()
    downloads = [c for c in calls if c["argv"][:2] == ["profile", "download"]]
    assert [c["argv"] for c in downloads] == [["profile", "download", "-a", "LPA:1$smdp.example.com$M"]] * 2
    # The second time without the check, and without a trust store
    assert downloads[0]["env"]["LPAC_HTTP_SSL_VERIFY"] == "1"
    assert downloads[1]["env"]["LPAC_HTTP_SSL_VERIFY"] == "0"
    assert "LPAC_HTTP_TRUST_STORE" not in downloads[1]["env"]
    assert r.value("wait_toasts") == ["Profile added. Switch it on to use it."]
    assert not r.warnings()


def test_unverified_server_cancelled(world, lpac):
    lpac["profile download"] = unverified_download(TLS_ERRORS[0])
    r = run(world, [*open_add(), set_text("LPA:1$smdp.example.com$M", type="AdwEntryRow",
                                          text="Activation Code"),
                    click(type="AdwButtonRow", text="Download Profile"),
                    wait_for(type="AdwAlertDialog"), click(type="GtkButton", text="Cancel"),
                    call("wait_toasts"), dump("after"), call("toasts")])
    assert r.value("wait_toasts") == [
        "Downloading the profile failed (The server could not be verified)"]
    assert lpac.argvs()[2:] == ["profile download -a LPA:1$smdp.example.com$M"]


def test_unverified_twice_is_not_asked_again(world, lpac):
    lpac["profile download"] = unverified_download(TLS_ERRORS[1])
    r = run(world, [*open_add(), set_text("LPA:1$smdp.example.com$M", type="AdwEntryRow",
                                          text="Activation Code"),
                    click(type="AdwButtonRow", text="Download Profile"),
                    wait_for(type="AdwAlertDialog"),
                    click(type="GtkButton", text="Continue Unverified"),
                    call("wait_toasts"), settle(200), dump("after"), call("toasts")])
    assert r.value("wait_toasts") == [
        "Downloading the profile failed (es9p_initiate_authentication)"]
    assert not r.dump("after").has(type="AdwAlertDialog")
    assert len([a for a in lpac.argvs() if a.startswith("profile download")]) == 2


def test_a_success_with_tls_noise_is_a_success(world, lpac):
    lpac["profile download"] = ok({"iccid": ICCID_B}, err=TLS_ERRORS[0])
    r = run(world, [*open_add(), set_text("LPA:1$smdp.example.com$M", type="AdwEntryRow",
                                          text="Activation Code"),
                    click(type="AdwButtonRow", text="Download Profile"),
                    call("wait_toasts"), dump("after"), call("toasts")])
    assert r.value("wait_toasts") == ["Profile added. Switch it on to use it."]


def test_closing_while_lpac_runs(world, lpac):
    lpac["chip info"] = ok(CHIP, sleep=0.5)
    r = run(world, [call("open", "/ril_0"), wait(100), call("close"), wait(1200), dump("after")],
            modem=None)
    assert not r.dump("after").has(type="CcEsimDialog")
    assert not r.warnings()


def test_closing_stops_the_dialogs_lpac_calls(world, lpac):
    lpac["chip info"] = ok(CHIP, sleep=0.5)
    run(world, [call("open", "/ril_0"), wait(100), call("close"), wait(1200)], modem=None)
    assert lpac.argvs() == ["chip info"]


# ----- Probing a modem -----

def ofono(world, modems=None, **replies):
    entries = ", ".join(
        f"(objectpath '{p}', {{'Serial': <'{serial}'>"
        + (f", 'Interfaces': <{ifaces!r}>" if ifaces is not None else "") + "})"
        for p, serial, ifaces in (modems or []))
    reply = f"([{entries}],)" if entries else "(@a(oa{sv}) [],)"
    world.service("system", OFONO, "/", OFONO_XML, replies={"GetModems": replies.get("get", reply)})


MODEMS = [("/ril_0", "350000000000001", ["org.ofono.SimManager"]),
          ("/ril_1", "350000000000002", ["org.ofono.SimManager", EUICC])]


def test_probe_finds_the_modem_with_an_euicc(world, lpac):
    ofono(world, MODEMS)
    r = run(world, [call("probe", "350000000000002")], modem=None)
    assert r.value("probe") == {"path": "/ril_1", "error": None}
    assert [c["argv"] for c in lpac.calls()] == [["chip", "info"]]
    assert lpac.calls()[0]["env"]["LPAC_APDU_OFONO_MODEM"] == "/ril_1"
    assert len(world.calls(OFONO, "GetModems")) == 1


@pytest.mark.parametrize("imei, modems", [
    ("350000000000001", MODEMS),                       # a plain modem without the channel
    ("350000000000009", MODEMS),                       # no such IMEI
    ("350000000000003", [("/ril_2", "350000000000003", None)]),  # no interfaces said
])
def test_probe_without_a_matching_modem(world, lpac, imei, modems):
    ofono(world, modems)
    r = run(world, [call("probe", imei)], modem=None)
    assert r.value("probe") == {"path": None, "error": None}
    assert not lpac.calls()


def test_probe_of_a_card_that_is_no_euicc(world, lpac):
    ofono(world, MODEMS)
    lpac["chip info"] = fail("es10c_get_eid", "6A82")
    r = run(world, [call("probe", "350000000000002")], modem=None)
    assert r.value("probe") == {"path": None, "error": None}


@pytest.mark.parametrize("imei", [None, ""])
def test_probe_without_an_imei_asks_nobody(world, lpac, imei):
    ofono(world, MODEMS)
    r = run(world, [call("probe", imei)], modem=None)
    assert r.value("probe") == {"path": None, "error": None}
    assert not world.calls(OFONO) and not lpac.calls()


def test_probe_without_lpac_asks_nobody(world, root):
    ofono(world, MODEMS)
    r = run(world, [call("probe", "350000000000002")], modem=None)
    assert r.value("probe") == {"path": None, "error": None}
    assert not world.calls(OFONO)


def test_probe_without_ofono_says_why(world, lpac):
    world.service("system", OFONO, "/", OFONO_XML, own=False)
    r = run(world, [call("probe", "350000000000002")], modem=None)
    assert r.value("probe")["path"] is None
    assert "org.ofono" in r.value("probe")["error"]


def test_probe_without_system_bus_says_why(world, lpac):
    r = run(world, [call("probe", "350000000000002")], modem=None, system_bus=False)
    assert r.value("probe")["path"] is None
    assert r.value("probe")["error"].startswith("Could not connect: ")


def test_probe_with_lpac_that_cannot_start(world, lpac):
    ofono(world, MODEMS)
    world.fake_bin.path.joinpath("lpac").write_text("#!/nonexistent/interpreter\n")
    r = run(world, [call("probe", "350000000000002")], modem=None)
    assert r.value("probe") == {
        "path": None, "error": "Failed to execute child process “lpac” (No such file or directory)"}


def test_probe_cancelled_while_lpac_runs(world, lpac):
    ofono(world, MODEMS)
    lpac["chip info"] = ok(CHIP, sleep=1)
    r = run(world, [call("probe", "350000000000002", 300)], modem=None)
    assert r.value("probe") == {"path": None, "error": "Operation was cancelled"}


# ----- Labels of the SIM cards -----

def test_labels_by_iccid_with_or_without_the_filler(world, root):
    r = run(world, [call("label_set", "89490200000123456789f", "  Work "),
                    call("label", "89490200000123456789"), call("labels"),
                    call("label_set", "89490200000123456789", "Home"),
                    call("label", "89490200000123456789FF"), call("labels", label="after"),
                    call("label_set", "8949020000012345678 9F", None),
                    call("label_set", " 89490200000123456789 ", None),
                    call("labels", label="removed")], modem=None)
    assert r.values("label") == ["Work", "Home"]
    assert r.value("labels") == "{'89490200000123456789': 'Work'}"
    assert r.value("after") == "{'89490200000123456789': 'Home'}"
    assert r.value("removed") == "{}"


@pytest.mark.parametrize("iccid", [None, "", "FFFF", "  "])
def test_labels_need_an_iccid(world, root, iccid):
    r = run(world, [call("label_set", iccid, "X"), call("label", iccid), call("labels")],
            modem=None)
    assert r.value("label") is None
    assert r.value("labels") == "{}"


def test_an_empty_label_is_no_label(world, root):
    r = run(world, [call("label_set", ICCID_A, ""), call("label", ICCID_A), call("labels")],
            modem=None)
    assert r.value("label") is None
    assert r.value("labels") == f"{{'{ICCID_A}': ''}}"


def test_without_the_schema_there_are_no_labels(world, lpac, tmp_path):
    # Schemas without org.plinius.sim
    import subprocess
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "org.plinius.test.other.gschema.xml").write_text(
        '<schemalist><schema id="org.plinius.test.other" path="/org/plinius/test/other/">'
        '<key name="x" type="b"><default>false</default></key></schema></schemalist>')
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    r = run(world, [loaded(), call("label_set", ICCID_A, "X"), call("label", ICCID_A),
                    call("labels"), dump("main")])
    assert r.value("label") is None and r.value("labels") is None
    # The nickname still names the profile
    assert [t for t, _s, _i in profile_rows(r.dump("main"))] == ["Telco Prepaid", "Travel"]
