# "Connect to Hidden Networks…" (cc-plinius-hidden-wifi.c): the first page (name, WPA/WPA2 or
# WPA3 Personal or none, password, adapter) and the page "Other Security" (WEP, LEAP, Dynamic WEP,
# WPA/WPA3 Enterprise with PEAP, TTLS, TLS, PWD, LEAP), against a fake NetworkManager
# (fake_nm.py) on the private system bus: which connection the pages hand NetworkManager, when
# an existing one is activated instead, which adapters are offered, what the pages say about wrong
# input and refused files. Certificates are chosen through a fake file chooser portal
# (fake_file_chooser.py).

import shutil
import subprocess

import pytest

from fake_file_chooser import FileChooserPortal
from fake_nm import NM_NAME, FakeNM
from gcc_harness import (build_panel, call, click, dump, enter, gcc_session, gcc_world,  # noqa: F401
                         select, set_active, set_text, settle, wait_for)

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-hidden-wifi.h"

static NMClient *client;

void
gcc_test_setup (GccTest *t)
{
    g_autoptr(GError) error = NULL;

    client = nm_client_new (NULL, &error);
    g_assert_no_error (error);
    if (!gcc_test_param (t, "no-dialog"))
        adw_dialog_present (cc_plinius_hidden_wifi_new (client), GTK_WIDGET (t->window));
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "new_without_client")) {
        json_builder_add_boolean_value (result, cc_plinius_hidden_wifi_new (NULL) == NULL);
        return TRUE;
    }
    if (g_str_equal (name, "dialogs")) {
        /* The titles of the dialogs open on the window, bottom first */
        GListModel *dialogs = adw_application_window_get_dialogs (ADW_APPLICATION_WINDOW (t->window));

        json_builder_begin_array (result);
        for (guint i = 0; i < g_list_model_get_n_items (dialogs); i++) {
            g_autoptr(AdwDialog) d = g_list_model_get_item (dialogs, i);

            json_builder_add_string_value (result, adw_dialog_get_title (d));
        }
        json_builder_end_array (result);
        return TRUE;
    }
    return FALSE;
}
'''

FIRST = "Connect to Hidden Wi-Fi Network"
OTHER = "Other Security"
WIFI = {"type": "CcPliniusHiddenWifi", "text": FIRST}
OTHER_DLG = {"type": "CcPliniusHiddenWifiOther", "text": OTHER}


NAME = {"type": "AdwEntryRow", "text": "Network name"}
SECURITY = {"type": "AdwComboRow", "text": "Wi-Fi security"}
PASSWORD = {"type": "AdwPasswordEntryRow", "text": "Password"}
ADAPTER = {"type": "AdwComboRow", "text": "Wi-Fi adapter"}
CONNECT = {"type": "GtkButton", "text": "Connect"}
NO_CA = {"type": "AdwSwitchRow", "text": "No CA certificate is required"}
DEV1 = "/org/freedesktop/NetworkManager/Devices/1"
DEV2 = "/org/freedesktop/NetworkManager/Devices/2"


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("hidden-wifi"), "hidden_wifi",
                       ["cc-plinius-hidden-wifi.c"], GLUE, pkgs=("libnm",),
                       upstream=("panels/network/panel-common.c", "panels/network/ui-helpers.c"))


@pytest.fixture
def nm(gcc_world):
    return FakeNM(gcc_world)


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    """A CA certificate, a user certificate with its key (PEM, encrypted with "secret"), both in
    a PKCS#12 file too (password "secret"), and a file that is none of them."""
    if not shutil.which("openssl"):
        pytest.skip("no openssl to make certificates")
    d = tmp_path_factory.mktemp("certs")

    def ssl(*args):
        subprocess.run(["openssl", *args], cwd=d, check=True, capture_output=True)

    ssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
        "-days", "30", "-subj", "/CN=Test CA")
    ssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "plain.key", "-out",
        "user.pem", "-days", "30", "-subj", "/CN=user")
    ssl("rsa", "-in", "plain.key", "-aes256", "-traditional", "-passout", "pass:secret", "-out",
        "user.key")
    ssl("pkcs12", "-export", "-in", "user.pem", "-inkey", "plain.key", "-passout", "pass:secret",
        "-out", "user.p12")
    (d / "notes.txt").write_text("not a certificate\n")
    return d


def run(world, program, nm, steps, portal=None, **kw):
    nm.start()
    if portal is not None:
        FileChooserPortal(world, portal).start()
    return world.run(program, steps, **kw)


def first(**sel):
    return dict(sel, within=WIFI)


def other(**sel):
    return dict(sel, within=OTHER_DLG)


OPEN_OTHER = [click(type="AdwActionRow", text=OTHER, within=WIFI), wait_for(**OTHER_DLG)]


def ssid(settings):
    return bytes(settings["802-11-wireless"]["ssid"]).decode()


# What libnm puts into every connection it sends (not the page's choice)
LIBNM_DEFAULTS = {("connection", "autoconnect-ports"): -1, ("connection", "permissions"): [],
                  ("802-11-wireless", "mac-address-denylist"): [],
                  # the old link to the security setting, set when there is one
                  ("802-11-wireless", "security"): "802-11-wireless-security"}


def page_settings(settings):
    return {name: {k: v for k, v in values.items() if LIBNM_DEFAULTS.get((name, k), object()) != v}
            for name, values in settings.items()}


def added(world):
    """The connections handed to AddAndActivateConnection: [(settings, device, specific)]."""
    return [(page_settings(c["args"][0]), c["args"][1], c["args"][2])
            for c in world.calls(NM_NAME, "AddAndActivateConnection")]


def connection_text(id_, uuid, name, security=None, extra=""):
    sec = f", '802-11-wireless-security': {{'key-mgmt': <'{security}'>}}" if security else ""
    # Bytes as they are (b'...' would end with a NUL)
    ssid = "[" + ", ".join(f"byte {b:#x}" for b in name.encode()) + "]"
    return (f"{{'connection': {{'id': <'{id_}'>, 'uuid': <'{uuid}'>, 'type': <'802-11-wireless'>}}, "
            f"'802-11-wireless': {{'ssid': <{ssid}>, 'mode': <'infrastructure'>, "
            f"'hidden': <true>}}{sec}{extra}}}")


def marked(screen, type_):
    return sorted(r["text"] for r in screen.find(type=type_) if "error" in r.get("classes", ()))


def connect_button(screen, within):
    return screen.within(**within).one(**CONNECT)


# ----- The first page -----

def test_first_page_with_one_adapter(program, gcc_world, nm):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [settle(), dump("page")])
    page = r.dump("page").within(**WIFI)
    assert page.find(type="AdwPreferencesGroup")[0]["subtitle"] == (
        "Enter the name and security details of the hidden Wi-Fi network you wish to connect to.")
    assert page.rows() == [("Network name", None), ("Wi-Fi security", None), ("Password", None),
                           ("Other Security", "WEP, LEAP or Enterprise (802.1X)")]
    sec = page.one(**SECURITY)
    assert sec["items"] == ["None", "WPA & WPA2 Personal", "WPA3 Personal"]
    assert sec["selected_text"] == "WPA & WPA2 Personal"
    # One adapter: nothing to choose; nothing filled in: no Connect
    assert not page.has(**ADAPTER)
    assert page.one(**CONNECT)["sensitive"] is False
    assert "suggested-action" in page.one(**CONNECT)["classes"]
    assert not page.has(text="Network name is too long")
    assert not added(gcc_world) and not gcc_world.calls(NM_NAME, "ActivateConnection")
    assert not r.warnings()


def test_wpa_personal_network_is_added_and_activated(program, gcc_world, nm):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [
        set_text("Hidden Home", **first(**NAME)), dump("name only"),
        set_text("correct horse", **first(**PASSWORD)), dump("filled"),
        click(**first(**CONNECT)), wait_for(gone=True, **WIFI), settle(200),
        call("dialogs")])
    assert connect_button(r.dump("name only"), WIFI)["sensitive"] is False
    assert connect_button(r.dump("filled"), WIFI).get("sensitive", True) is True
    [(settings, device, specific)] = added(gcc_world)
    assert device == DEV1 and specific == "/"
    assert settings == {
        "connection": {"id": "Hidden Home", "type": "802-11-wireless"},
        "802-11-wireless": {"ssid": list(b"Hidden Home"), "mode": "infrastructure", "hidden": True},
        "802-11-wireless-security": {"key-mgmt": "wpa-psk", "psk": "correct horse"},
    }
    # The page closed itself
    assert r.value("dialogs") == []
    # NetworkManager's answer came: no complaint
    assert not r.warnings()


def test_wpa3_takes_a_password_of_any_length(program, gcc_world, nm):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [
        set_text("Lab", **first(**NAME)), select(2, **first(**SECURITY)), dump("empty"),
        set_text("abc", **first(**PASSWORD)), dump("short"),
        click(**first(**CONNECT)), settle(200)])
    assert connect_button(r.dump("empty"), WIFI)["sensitive"] is False
    # Empty is not wrong yet
    assert marked(r.dump("empty"), "AdwPasswordEntryRow") == []
    assert marked(r.dump("short"), "AdwPasswordEntryRow") == []
    [(settings, _d, _s)] = added(gcc_world)
    assert settings["802-11-wireless-security"] == {"key-mgmt": "sae", "psk": "abc"}


def test_open_network_needs_no_password(program, gcc_world, nm):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [
        set_text("short", **first(**PASSWORD)),
        set_text("Cafe", **first(**NAME)), select(0, **first(**SECURITY)), dump("page"),
        click(**first(**CONNECT)), settle(200)])
    page = r.dump("page").within(**WIFI)
    assert not page.has(**PASSWORD)
    assert page.one(**CONNECT).get("sensitive", True) is True
    [(settings, _d, _s)] = added(gcc_world)
    assert ssid(settings) == "Cafe"
    assert "802-11-wireless-security" not in settings


@pytest.mark.parametrize("password, ok", [
    ("1234567", False), ("12345678", True), ("x" * 63, True), ("x" * 64, False),
    ("0123456789abcdef" * 4, True), ("0123456789abcdeg" * 4, False)])
def test_wpa_password_length_and_hex_key(program, gcc_world, nm, password, ok):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [set_text("Net", **first(**NAME)),
                                     set_text(password, **first(**PASSWORD)), dump("page")])
    screen = r.dump("page")
    assert marked(screen, "AdwPasswordEntryRow") == ([] if ok else ["Password"])
    assert (connect_button(screen, WIFI).get("sensitive", True) is True) == ok


@pytest.mark.parametrize("name, ok", [("x" * 32, True), ("x" * 33, False),
                                      ("ä" * 16, True), ("ä" * 17, False)])
def test_network_name_of_at_most_32_bytes(program, gcc_world, nm, name, ok):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [set_text("12345678", **first(**PASSWORD)),
                                     set_text(name, **first(**NAME)), dump("page")])
    screen = r.dump("page").within(**WIFI)
    assert screen.has(type="GtkLabel", text="Network name is too long") == (not ok)
    assert marked(screen, "AdwEntryRow") == ([] if ok else ["Network name"])
    assert (screen.one(**CONNECT).get("sensitive", True) is True) == ok


def test_too_long_name_mark_goes_when_shortened(program, gcc_world, nm):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [set_text("x" * 40, **first(**NAME)),
                                     set_text("x" * 10, **first(**NAME)), dump("page")])
    screen = r.dump("page").within(**WIFI)
    assert not screen.has(text="Network name is too long")
    assert marked(screen, "AdwEntryRow") == []


def test_enter_in_a_field_connects_when_complete(program, gcc_world, nm):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [
        set_text("Net", **first(**NAME)),
        # Not complete: Enter does nothing
        enter(**first(**NAME)), settle(100), call("dialogs", label="open"),
        set_text("12345678", **first(**PASSWORD)), enter(**first(**PASSWORD)), settle(200),
        call("dialogs", label="after")])
    assert r.value("open") == [FIRST]
    assert r.value("after") == []
    assert len(added(gcc_world)) == 1


def test_enter_in_the_name_field_connects(program, gcc_world, nm):
    nm.device("wlan0")
    run(gcc_world, program, nm, [set_text("12345678", **first(**PASSWORD)),
                                 set_text("Net", **first(**NAME)), enter(**first(**NAME)),
                                 settle(200)])
    assert [ssid(s) for s, _d, _x in added(gcc_world)] == ["Net"]


def test_only_managed_client_wifi_adapters_are_offered(program, gcc_world, nm):
    nm.device("eth0", type="ethernet")
    nm.device("ap0")              # only an access point (MediaTek)
    nm.device("wlan1", managed=False)
    nm.device("apx")              # "ap" without a number: a client interface
    nm.device("ap")
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [set_text("Net", **first(**NAME)),
                                     set_text("12345678", **first(**PASSWORD)), dump("page"),
                                     select(item="wlan0", **first(**ADAPTER)),
                                     click(**first(**CONNECT)), settle(200)])
    adapter = r.dump("page").within(**WIFI).one(**ADAPTER)
    assert adapter["items"] == ["apx", "ap", "wlan0"]
    assert adapter["selected_text"] == "apx"
    [(_s, device, _x)] = added(gcc_world)
    assert device == "/org/freedesktop/NetworkManager/Devices/6"


def test_two_adapters_the_chosen_one_connects(program, gcc_world, nm):
    nm.device("wlan0")
    nm.device("wlan1")
    r = run(gcc_world, program, nm, [set_text("Net", **first(**NAME)),
                                     set_text("12345678", **first(**PASSWORD)),
                                     select(1, **first(**ADAPTER)), dump("page"),
                                     click(**first(**CONNECT)), settle(200)])
    assert r.dump("page").within(**WIFI).one(**ADAPTER)["items"] == ["wlan0", "wlan1"]
    assert [d for _s, d, _x in added(gcc_world)] == [DEV2]


def test_without_a_wifi_adapter_there_is_no_connect(program, gcc_world, nm):
    nm.device("eth0", type="ethernet")
    nm.device("ap0")
    r = run(gcc_world, program, nm, [set_text("Net", **first(**NAME)),
                                     set_text("12345678", **first(**PASSWORD)),
                                     enter(**first(**PASSWORD)), settle(100), dump("page")])
    page = r.dump("page").within(**WIFI)
    assert not page.has(**ADAPTER)
    assert page.one(**CONNECT)["sensitive"] is False
    assert not added(gcc_world)


def test_a_matching_connection_is_activated_instead(program, gcc_world, nm):
    nm.device("wlan0")
    # A connection for another network, and the one for this network (under another name)
    nm.connection(connection_text("Office", "8a2b0e2c-0000-4000-8000-000000000001", "Office"))
    path = nm.connection(connection_text("My Cafe", "8a2b0e2c-0000-4000-8000-000000000002", "Cafe"))
    r = run(gcc_world, program, nm, [set_text("Cafe", **first(**NAME)), select(0, **first(**SECURITY)),
                                     click(**first(**CONNECT)), settle(300), call("dialogs")])
    assert not added(gcc_world)
    assert [c["args"] for c in gcc_world.calls(NM_NAME, "ActivateConnection")] == [[path, DEV1, "/"]]
    assert r.value("dialogs") == []
    assert not r.warnings()


def test_a_connection_with_other_security_is_not_taken(program, gcc_world, nm):
    nm.device("wlan0")
    nm.connection(connection_text("Lab", "8a2b0e2c-0000-4000-8000-000000000002", "Lab", "sae"))
    run(gcc_world, program, nm, [set_text("Lab", **first(**NAME)), select(0, **first(**SECURITY)),
                                 click(**first(**CONNECT)), settle(200)])
    assert not gcc_world.calls(NM_NAME, "ActivateConnection")
    assert len(added(gcc_world)) == 1


def test_a_saved_wpa_connection_of_the_network_is_activated(program, gcc_world, nm):
    # NetworkManager gives no secrets: the comparison leaves them out
    nm.device("wlan0")
    path = nm.connection(connection_text("Lab", "8a2b0e2c-0000-4000-8000-000000000002", "Lab",
                                         "wpa-psk"))
    run(gcc_world, program, nm, [set_text("Lab", **first(**NAME)),
                                 set_text("12345678", **first(**PASSWORD)),
                                 click(**first(**CONNECT)), settle(300)])
    assert [c["args"] for c in gcc_world.calls(NM_NAME, "ActivateConnection")] == [[path, DEV1, "/"]]
    assert not added(gcc_world)


def test_refused_activation_is_logged(program, gcc_world, nm):
    nm.device("wlan0")
    nm.connection(connection_text("Cafe", "8a2b0e2c-0000-4000-8000-000000000002", "Cafe"))
    nm.reply("ActivateConnection", "org.freedesktop.NetworkManager.UnknownDevice", "No such device")
    r = run(gcc_world, program, nm, [set_text("Cafe", **first(**NAME)), select(0, **first(**SECURITY)),
                                     click(**first(**CONNECT)), settle(300)])
    [w] = r.warnings()
    assert w["message"].startswith("Failed to activate connection: (")
    assert w["message"].endswith("No such device")


def test_refused_new_connection_is_logged(program, gcc_world, nm):
    nm.device("wlan0")
    nm.reply("AddAndActivateConnection", "org.freedesktop.NetworkManager.Settings.InvalidProperty",
             "802-11-wireless.ssid: bad")
    r = run(gcc_world, program, nm, [set_text("Net", **first(**NAME)),
                                     set_text("12345678", **first(**PASSWORD)),
                                     click(**first(**CONNECT)), settle(300)])
    [w] = r.warnings()
    assert w["message"].startswith("Failed to add new connection: (")
    assert w["message"].endswith("802-11-wireless.ssid: bad")


def test_without_a_client_there_is_no_page(program, gcc_world, nm):
    r = run(gcc_world, program, nm, [call("new_without_client")], params={"no-dialog": "1"})
    assert r.value("new_without_client") is True
    [w] = r.warnings()
    assert "NM_IS_CLIENT (client)" in w["message"]


# ----- Other Security -----

def other_page(r, label):
    return r.dump(label).within(**OTHER_DLG)


def test_other_security_opens_above_with_the_same_name(program, gcc_world, nm):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [set_text("Corp", **first(**NAME)), *OPEN_OTHER,
                                     call("dialogs"), dump("other"),
                                     set_text("Corp2", **other(**NAME)), settle(),
                                     {"op": "close_dialog"}, settle(), dump("first")])
    assert r.value("dialogs") == [FIRST, OTHER]
    page = other_page(r, "other")
    sec = page.one(**SECURITY)
    assert sec["items"] == ["WEP 40/128-bit Key (Hex or ASCII)", "WEP 128-bit Passphrase", "LEAP",
                            "Dynamic WEP (802.1x)", "WPA & WPA2 Enterprise", "WPA3 Enterprise"]
    assert sec["selected_text"] == "WPA & WPA2 Enterprise"
    assert page.one(**NAME)["value"] == "Corp"
    # PEAP first, with what it asks
    assert [t for t, _s in page.rows()] == [
        "Network name", "Wi-Fi security", "Authentication", "Inner authentication",
        "Anonymous identity", "Username", "Password", "CA certificate",
        "No CA certificate is required", "Domain", "PEAP version"]
    assert page.one(type="AdwComboRow", text="Authentication")["items"] == [
        "Protected EAP (PEAP)", "Tunneled TLS", "TLS", "PWD", "LEAP"]
    assert page.one(type="AdwComboRow", text="Inner authentication")["items"] == [
        "MSCHAPv2", "MD5", "GTC"]
    assert page.one(type="AdwComboRow", text="PEAP version")["items"] == [
        "Automatic", "Version 0", "Version 1"]
    assert page.one(type="AdwActionRow", text="CA certificate")["subtitle"] == "(None)"
    assert page.one(type="AdwEntryRow", text="Domain")["tooltip"] == "Suffix of the server certificate name."
    assert page.one(**CONNECT)["sensitive"] is False
    # Back: the first page again, with the name typed above
    assert r.dump("first").within(**WIFI).one(**NAME)["value"] == "Corp2"
    assert not added(gcc_world)


def test_peap_without_ca_certificate(program, gcc_world, nm):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [
        set_text("Corp", **first(**NAME)), *OPEN_OTHER,
        set_text("alice", **other(type="AdwEntryRow", text="Username")),
        set_text("pw", **other(type="AdwPasswordEntryRow", text="Password")), dump("no ca"),
        set_active(True, **other(**NO_CA)), dump("ready"),
        click(**other(**CONNECT)), settle(300), call("dialogs")])
    # Without a CA certificate or the switch: no Connect
    assert other_page(r, "no ca").one(**CONNECT)["sensitive"] is False
    ready = other_page(r, "ready")
    assert ready.one(**CONNECT).get("sensitive", True) is True
    # The switch makes the certificate row insensitive
    assert ready.one(type="AdwActionRow", text="CA certificate")["sensitive"] is False
    [(settings, device, _x)] = added(gcc_world)
    assert device == DEV1
    assert settings["802-11-wireless-security"] == {"key-mgmt": "wpa-eap"}
    assert settings["802-1x"] == {"eap": ["peap"], "identity": "alice", "password": "pw",
                                  "phase2-auth": "mschapv2"}
    assert ssid(settings) == "Corp" and settings["802-11-wireless"]["hidden"] is True
    # Both pages went
    assert r.value("dialogs") == []
    assert not r.warnings()


@pytest.mark.parametrize("version, peapver", [(1, "0"), (2, "1")])
def test_peap_version_domain_and_anonymous_identity(program, gcc_world, nm, version, peapver):
    nm.device("wlan0")
    run(gcc_world, program, nm, [
        set_text("Corp", **first(**NAME)), *OPEN_OTHER,
        select(2, **other(type="AdwComboRow", text="Inner authentication")),
        set_text("anon", **other(type="AdwEntryRow", text="Anonymous identity")),
        set_text("alice", **other(type="AdwEntryRow", text="Username")),
        set_text("pw", **other(type="AdwPasswordEntryRow", text="Password")),
        set_active(True, **other(**NO_CA)),
        set_text("radius.example.org", **other(type="AdwEntryRow", text="Domain")),
        select(version, **other(type="AdwComboRow", text="PEAP version")),
        click(**other(**CONNECT)), settle(300)])
    [(settings, _d, _x)] = added(gcc_world)
    assert settings["802-1x"] == {"eap": ["peap"], "identity": "alice", "password": "pw",
                                  "anonymous-identity": "anon", "phase2-auth": "gtc",
                                  "phase1-peapver": peapver,
                                  "domain-suffix-match": "radius.example.org"}


@pytest.mark.parametrize("index, key, method", [(0, "phase2-auth", "pap"),
                                                (2, "phase2-auth", "mschapv2"),
                                                (4, "phase2-autheap", "md5"),
                                                (6, "phase2-autheap", "mschapv2")])
def test_ttls_inner_methods(program, gcc_world, nm, index, key, method):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [
        set_text("Corp", **first(**NAME)), *OPEN_OTHER,
        select(1, **other(type="AdwComboRow", text="Authentication")), dump("ttls"),
        select(index, **other(type="AdwComboRow", text="Inner authentication")),
        set_text("bob", **other(type="AdwEntryRow", text="Username")),
        set_text("pw", **other(type="AdwPasswordEntryRow", text="Password")),
        set_active(True, **other(**NO_CA)),
        click(**other(**CONNECT)), settle(300)])
    page = other_page(r, "ttls")
    inner = page.one(type="AdwComboRow", text="Inner authentication")
    assert inner["items"] == ["PAP", "MSCHAP", "MSCHAPv2 (no EAP)", "CHAP", "MD5", "GTC", "MSCHAPv2"]
    assert inner["selected_text"] == "PAP"
    assert not page.has(type="AdwComboRow", text="PEAP version")
    [(settings, _d, _x)] = added(gcc_world)
    assert settings["802-1x"]["eap"] == ["ttls"]
    assert settings["802-1x"][key] == method
    assert not ({"phase2-auth", "phase2-autheap"} - {key}) & set(settings["802-1x"])


def test_inner_choice_stays_while_the_method_stays(program, gcc_world, nm):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [
        set_text("Corp", **first(**NAME)), *OPEN_OTHER,
        select(1, **other(type="AdwComboRow", text="Inner authentication")),
        # Another change of the form keeps it
        set_text("bob", **other(type="AdwEntryRow", text="Username")), dump("kept"),
        # TTLS and back to PEAP: the list anew, its first entry
        select(1, **other(type="AdwComboRow", text="Authentication")),
        select(0, **other(type="AdwComboRow", text="Authentication")), dump("again")])
    assert other_page(r, "kept").one(type="AdwComboRow", text="Inner authentication")["selected_text"] == "MD5"
    assert other_page(r, "again").one(type="AdwComboRow", text="Inner authentication")["selected_text"] == "MSCHAPv2"


@pytest.mark.parametrize("index, eap", [(3, "pwd"), (4, "leap")])
def test_pwd_and_leap_need_no_server_check(program, gcc_world, nm, index, eap):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [
        set_text("Corp", **first(**NAME)), *OPEN_OTHER,
        select(index, **other(type="AdwComboRow", text="Authentication")),
        set_text("bob", **other(type="AdwEntryRow", text="Username")), dump("no password"),
        set_text("pw", **other(type="AdwPasswordEntryRow", text="Password")), dump("page"),
        click(**other(**CONNECT)), settle(300)])
    page = other_page(r, "page")
    assert [t for t, _s in page.rows()] == ["Network name", "Wi-Fi security", "Authentication",
                                            "Username", "Password"]
    assert other_page(r, "no password").one(**CONNECT)["sensitive"] is False
    assert page.one(**CONNECT).get("sensitive", True) is True
    [(settings, _d, _x)] = added(gcc_world)
    assert settings["802-1x"] == {"eap": [eap], "identity": "bob", "password": "pw"}


@pytest.mark.parametrize("index, security", [
    (3, {"key-mgmt": "ieee8021x", "auth-alg": "open"}),
    (5, {"key-mgmt": "wpa-eap", "pmf": 3})])
def test_dynamic_wep_and_wpa3_enterprise(program, gcc_world, nm, index, security):
    nm.device("wlan0")
    run(gcc_world, program, nm, [
        set_text("Corp", **first(**NAME)), *OPEN_OTHER,
        select(index, **other(**SECURITY)),
        select(3, **other(type="AdwComboRow", text="Authentication")),
        set_text("bob", **other(type="AdwEntryRow", text="Username")),
        set_text("pw", **other(type="AdwPasswordEntryRow", text="Password")),
        click(**other(**CONNECT)), settle(300)])
    [(settings, _d, _x)] = added(gcc_world)
    assert settings["802-11-wireless-security"] == security
    assert settings["802-1x"]["eap"] == ["pwd"]


def test_leap(program, gcc_world, nm):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [
        set_text("Old", **first(**NAME)), *OPEN_OTHER,
        select(2, **other(**SECURITY)), dump("leap"),
        set_text("carol", **other(type="AdwEntryRow", text="Username")), dump("half"),
        set_text("pw", **other(type="AdwPasswordEntryRow", text="Password")),
        click(**other(**CONNECT)), settle(300)])
    assert [t for t, _s in other_page(r, "leap").rows()] == ["Network name", "Wi-Fi security",
                                                             "Username", "Password"]
    assert other_page(r, "half").one(**CONNECT)["sensitive"] is False
    [(settings, _d, _x)] = added(gcc_world)
    assert settings["802-11-wireless-security"] == {
        "key-mgmt": "ieee8021x", "auth-alg": "leap", "leap-username": "carol", "leap-password": "pw"}
    assert "802-1x" not in settings


@pytest.mark.parametrize("security, key, ok", [
    (0, "0123456789", True), (0, "abcde", True), (0, "0123456789abcdef0123456789", True),
    (0, "123", False), (0, "012345678g", False),
    (1, "any passphrase", True), (1, "x" * 65, False)])
def test_wep_key_kinds(program, gcc_world, nm, security, key, ok):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [
        set_text("Old", **first(**NAME)), *OPEN_OTHER,
        select(security, **other(**SECURITY)),
        set_text(key, **other(type="AdwPasswordEntryRow", text="Key")), dump("page")])
    page = other_page(r, "page")
    assert [t for t, _s in page.rows()] == ["Network name", "Wi-Fi security", "Key", "WEP index",
                                            "Authentication"]
    assert marked(page, "AdwPasswordEntryRow") == ([] if ok else ["Key"])
    assert (page.one(**CONNECT).get("sensitive", True) is True) == ok


@pytest.mark.parametrize("security, index, auth, expect", [
    (0, 0, 0, {"wep-key-type": 1, "auth-alg": "open", "wep-key0": "0123456789"}),
    (0, 3, 1, {"wep-key-type": 1, "auth-alg": "shared", "wep-tx-keyidx": 3,
               "wep-key3": "0123456789"}),
    (1, 1, 0, {"wep-key-type": 2, "auth-alg": "open", "wep-tx-keyidx": 1,
               "wep-key1": "0123456789"})])
def test_wep_connection(program, gcc_world, nm, security, index, auth, expect):
    nm.device("wlan0")
    run(gcc_world, program, nm, [
        set_text("Old", **first(**NAME)), *OPEN_OTHER,
        select(security, **other(**SECURITY)),
        set_text("0123456789", **other(type="AdwPasswordEntryRow", text="Key")),
        select(index, **other(type="AdwComboRow", text="WEP index")),
        select(auth, **other(type="AdwComboRow", text="Authentication")),
        enter(**other(type="AdwPasswordEntryRow", text="Key")), settle(300)])
    [(settings, _d, _x)] = added(gcc_world)
    assert settings["802-11-wireless-security"] == {"key-mgmt": "none", **expect}


def test_other_page_without_an_adapter_never_connects(program, gcc_world, nm):
    nm.device("ap0")
    r = run(gcc_world, program, nm, [
        set_text("Old", **first(**NAME)), *OPEN_OTHER,
        select(2, **other(**SECURITY)),
        set_text("carol", **other(type="AdwEntryRow", text="Username")),
        set_text("pw", **other(type="AdwPasswordEntryRow", text="Password")),
        enter(**other(type="AdwPasswordEntryRow", text="Password")), settle(100), dump("page")])
    assert other_page(r, "page").one(**CONNECT)["sensitive"] is False
    assert not added(gcc_world)


def test_too_long_name_on_the_other_page(program, gcc_world, nm):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [*OPEN_OTHER, set_text("y" * 33, **other(**NAME)), dump("page")])
    page = other_page(r, "page")
    assert page.has(type="GtkLabel", text="Network name is too long")
    # The first page has the same name and says it too
    assert r.dump("page").within(**WIFI).has(type="GtkLabel", text="Network name is too long")


# ----- TLS and certificate files -----

def uri(path):
    return "file://" + str(path)


TLS_STEPS = [set_text("Corp", **first(**NAME)), *OPEN_OTHER,
             select(2, **other(type="AdwComboRow", text="Authentication"))]


def choose(row):
    return [click(**other(type="AdwActionRow", text=row)), settle(300)]


def test_tls_rows(program, gcc_world, nm):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [*TLS_STEPS, dump("page")])
    page = other_page(r, "page")
    assert [(t, s) for t, s in page.rows()] == [
        ("Network name", None), ("Wi-Fi security", None), ("Authentication", None),
        ("Identity", None), ("User certificate", "No certificate set"),
        ("User private key", "No key set"), ("User key password", None),
        ("CA certificate", "(None)"), ("No CA certificate is required", None), ("Domain", None)]


def test_tls_with_certificate_key_and_ca(program, gcc_world, nm, certs):
    nm.device("wlan0")
    portal = [{"uris": [uri(certs / "user.pem")]}, {"uris": [uri(certs / "user.key")]},
              {"uris": [uri(certs / "ca.pem")]}]
    r = run(gcc_world, program, nm, [
        *TLS_STEPS,
        set_text("alice@example.org", **other(type="AdwEntryRow", text="Identity")),
        *choose("User certificate"), *choose("User private key"), dump("no ca"),
        *choose("CA certificate"),
        set_text("secret", **other(type="AdwPasswordEntryRow", text="User key password")),
        dump("page"), click(**other(**CONNECT)), settle(300), call("dialogs")], portal=portal)
    assert other_page(r, "no ca").one(**CONNECT)["sensitive"] is False
    page = other_page(r, "page")
    assert page.one(type="AdwActionRow", text="User certificate")["subtitle"] == "user.pem"
    assert page.one(type="AdwActionRow", text="User private key")["subtitle"] == "user.key"
    assert page.one(type="AdwActionRow", text="CA certificate")["subtitle"] == "ca.pem"
    assert page.one(**CONNECT).get("sensitive", True) is True
    [(settings, _d, _x)] = added(gcc_world)
    x = settings["802-1x"]
    assert x["eap"] == ["tls"] and x["identity"] == "alice@example.org"
    assert "password" not in x
    assert bytes(x["client-cert"]) == b"file://" + bytes(certs / "user.pem") + b"\0"
    assert bytes(x["private-key"]) == b"file://" + bytes(certs / "user.key") + b"\0"
    assert bytes(x["ca-cert"]) == b"file://" + bytes(certs / "ca.pem") + b"\0"
    assert x["private-key-password"] == "secret"
    assert r.value("dialogs") == []
    # The portal was asked with libnma's titles and filters
    opens = [c["args"] for c in gcc_world.calls("org.freedesktop.portal.Desktop", "OpenFile")]
    assert [o[1] for o in opens] == ["Choose a User Certificate", "Choose a key for User Certificate",
                                     "Choose a CA Certificate"]
    assert [[f[0] for f in o[2]["filters"]] for o in opens] == [
        ["PEM certificates (*.pem, *.crt, *.cer)",
         "DER, PEM, or PKCS#12 private keys (*.der, *.pem, *.p12, *.key)", "All files"],
        ["DER, PEM, or PKCS#12 private keys (*.der, *.pem, *.p12, *.key)", "All files"],
        ["PEM certificates (*.pem, *.crt, *.cer)", "All files"]]


def test_wrong_key_password_is_said_on_the_page(program, gcc_world, nm, certs):
    # libnm checks the password of a PKCS#12 file (not of a PEM key)
    nm.device("wlan0")
    portal = [{"uris": [uri(certs / "user.p12")]}]
    r = run(gcc_world, program, nm, [
        *TLS_STEPS,
        set_text("alice", **other(type="AdwEntryRow", text="Identity")),
        *choose("User certificate"),
        set_active(True, **other(**NO_CA)),
        set_text("wrong", **other(type="AdwPasswordEntryRow", text="User key password")),
        click(**other(**CONNECT)), settle(300), dump("page"), call("dialogs"),
        # Any change: the message is old
        set_text("secret", **other(type="AdwPasswordEntryRow", text="User key password")),
        dump("changed")], portal=portal)
    assert not added(gcc_world)
    assert r.value("dialogs") == [FIRST, OTHER]
    labels = [n["text"] for n in other_page(r, "page").find(type="GtkLabel")
              if "dim-label" in n.get("classes", ())]
    assert len(labels) == 1 and labels[0]
    assert not [n for n in other_page(r, "changed").find(type="GtkLabel", text=labels[0])]


def test_pkcs12_file_is_certificate_and_key(program, gcc_world, nm, certs):
    nm.device("wlan0")
    portal = [{"uris": [uri(certs / "user.pem")]}, {"uris": [uri(certs / "user.key")]},
              {"uris": [uri(certs / "user.p12")]}, {"uris": [uri(certs / "user.pem")]}]
    r = run(gcc_world, program, nm, [
        *TLS_STEPS,
        set_text("alice", **other(type="AdwEntryRow", text="Identity")),
        *choose("User certificate"), *choose("User private key"),
        *choose("User certificate"), dump("p12"),
        set_active(True, **other(**NO_CA)),
        set_text("secret", **other(type="AdwPasswordEntryRow", text="User key password")),
        dump("ready"),
        # A plain certificate again: the PKCS#12 file's key does not go with it
        *choose("User certificate"), dump("pem")], portal=portal)
    p12 = other_page(r, "p12")
    assert p12.one(type="AdwActionRow", text="User certificate")["subtitle"] == "user.p12"
    # The key is in the file: no row for it
    assert not p12.has(type="AdwActionRow", text="User private key")
    assert other_page(r, "ready").one(**CONNECT).get("sensitive", True) is True
    pem = other_page(r, "pem")
    assert pem.one(type="AdwActionRow", text="User certificate")["subtitle"] == "user.pem"
    assert pem.one(type="AdwActionRow", text="User private key")["subtitle"] == "No key set"
    assert pem.one(**CONNECT)["sensitive"] is False
    # Choosing again starts at the file chosen before
    opens = [c["args"][2] for c in gcc_world.calls("org.freedesktop.portal.Desktop", "OpenFile")]
    assert "current_folder" not in opens[0]
    assert bytes(opens[3]["current_folder"]).rstrip(b"\0") == bytes(certs)


def test_pkcs12_connection(program, gcc_world, nm, certs):
    nm.device("wlan0")
    portal = [{"uris": [uri(certs / "user.p12")]}]
    run(gcc_world, program, nm, [
        *TLS_STEPS,
        set_text("alice", **other(type="AdwEntryRow", text="Identity")),
        *choose("User private key"),
        set_active(True, **other(**NO_CA)),
        set_text("secret", **other(type="AdwPasswordEntryRow", text="User key password")),
        click(**other(**CONNECT)), settle(300)], portal=portal)
    [(settings, _d, _x)] = added(gcc_world)
    x = settings["802-1x"]
    p12 = b"file://" + bytes(certs / "user.p12") + b"\0"
    assert bytes(x["client-cert"]) == p12 and bytes(x["private-key"]) == p12
    assert "ca-cert" not in x


def test_a_file_that_is_no_certificate_is_refused(program, gcc_world, nm, certs):
    nm.device("wlan0")
    portal = [{"uris": [uri(certs / "notes.txt")]}, {"uris": [uri(certs / "ca.pem")]}]
    r = run(gcc_world, program, nm, [
        *TLS_STEPS, *choose("CA certificate"), dump("refused"),
        *choose("CA certificate"), dump("taken")], portal=portal)
    refused = other_page(r, "refused")
    ca = refused.one(type="AdwActionRow", text="CA certificate")
    assert "error" in ca["classes"] and ca["subtitle"] == "(None)"
    messages = [n["text"] for n in refused.find(type="GtkLabel") if "dim-label" in n.get("classes", ())]
    assert len(messages) == 1 and messages[0]
    taken = other_page(r, "taken")
    ca = taken.one(type="AdwActionRow", text="CA certificate")
    assert "error" not in ca.get("classes", ()) and ca["subtitle"] == "ca.pem"
    assert not taken.has(type="GtkLabel", text=messages[0])


def test_ca_file_gone_before_connecting(program, gcc_world, nm, certs, tmp_path):
    nm.device("wlan0")
    ca = tmp_path / "gone.pem"
    shutil.copy(certs / "ca.pem", ca)
    portal = [{"uris": [uri(ca)]}]
    r = run(gcc_world, program, nm, [
        set_text("Corp", **first(**NAME)), *OPEN_OTHER,
        set_text("alice", **other(type="AdwEntryRow", text="Username")),
        set_text("pw", **other(type="AdwPasswordEntryRow", text="Password")),
        *choose("CA certificate"),
        {"op": "remove_file", "path": str(ca)},
        click(**other(**CONNECT)), settle(300), dump("page"), call("dialogs")], portal=portal)
    assert not added(gcc_world)
    assert r.value("dialogs") == [FIRST, OTHER]
    messages = [n["text"] for n in other_page(r, "page").find(type="GtkLabel")
                if "dim-label" in n.get("classes", ())]
    assert len(messages) == 1 and messages[0]


def test_user_certificate_gone_before_connecting(program, gcc_world, nm, certs, tmp_path):
    nm.device("wlan0")
    cert = tmp_path / "gone.pem"
    shutil.copy(certs / "user.pem", cert)
    portal = [{"uris": [uri(cert)]}, {"uris": [uri(certs / "user.key")]}]
    r = run(gcc_world, program, nm, [
        *TLS_STEPS, set_text("alice", **other(type="AdwEntryRow", text="Identity")),
        *choose("User certificate"), *choose("User private key"),
        set_active(True, **other(**NO_CA)),
        {"op": "remove_file", "path": str(cert)},
        click(**other(**CONNECT)), settle(300), call("dialogs")], portal=portal)
    assert not added(gcc_world)
    assert r.value("dialogs") == [FIRST, OTHER]


# Not tested: a dismissed or failed chooser (file_opened_cb, the last else-if). With GLib 2.88.3 and
# GTK 4.22.5 on the host, GtkFileDialog's g_task_return_new_error () from the portal's Response
# hangs the test program (the main thread waits for its context's lock in g_source_get_time ()).
def test_chosen_file_without_a_local_path(program, gcc_world, nm):
    # NetworkManager reads the file itself: a file of no path is not taken
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [*TLS_STEPS, *choose("CA certificate"), dump("page")],
            portal=[{"uris": ["http://example.org/ca.pem"]}])
    page = other_page(r, "page")
    assert page.one(type="AdwActionRow", text="CA certificate")["subtitle"] == "(None)"
    assert [w["message"] for w in r.warnings()] == ["Chosen certificate has no local path"]


def test_an_answer_after_the_page_closed_is_harmless(program, gcc_world, nm, certs):
    nm.device("wlan0")
    r = run(gcc_world, program, nm, [
        *TLS_STEPS, click(**other(type="AdwActionRow", text="CA certificate")),
        {"op": "close_dialog"}, settle(), call("dialogs", label="closed"), wait_for_portal_answer(),
        call("dialogs", label="after"), dump("page")],
        portal=[{"uris": [uri(certs / "ca.pem")], "delay_ms": 500}])
    assert r.value("closed") == [FIRST] and r.value("after") == [FIRST]
    assert r.dump("page").within(**WIFI).one(**NAME)["value"] == "Corp"
    assert not r.warnings()


def wait_for_portal_answer():
    return {"op": "wait", "ms": 900}
