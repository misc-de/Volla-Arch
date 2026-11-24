# plinius-satellite: which network counts as a satellite (the shipped providers.conf, local
# overrides), the forecast for scanned networks, and what happens when a modem comes onto a
# satellite and leaves it again (state, roaming data on and off with its state file,
# hysteresis). oFono and NetworkManager calls are answered by a fake, nothing reaches D-Bus.

import functools
import shutil

import pytest

from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "plinius-satellite" / "plinius-satellite.c"
PROVIDERS = PKG / "plinius-satellite" / "providers.conf"

HARNESS = r"""
#include <glib.h>
#include <gio/gio.h>
#include <stdio.h>
#include <stdlib.h>
static gboolean fake_emit_signal(GDBusConnection *c, const char *dest, const char *path, const char *iface,
				 const char *name, GVariant *params, GError **error)
{
	gchar *text = g_variant_print(g_variant_ref_sink(params), FALSE);
	printf("signal=%s\n", text);
	g_free(text);
	g_variant_unref(params);
	return TRUE;
}
/* oFono and NetworkManager: logged; SetProperty succeeds, NetworkManager has no devices */
static GVariant *fake_call_sync(GDBusConnection *c, const char *dest, const char *path, const char *iface,
				const char *method, GVariant *args, const GVariantType *reply_type,
				GDBusCallFlags flags, int timeout, GCancellable *cancel, GError **error)
{
	gchar *text = args ? g_variant_print(g_variant_ref_sink(args), FALSE) : g_strdup("");
	printf("call=%s %s %s%s\n", path, iface, method, text);
	g_free(text);
	if (args)
		g_variant_unref(args);
	if (!strcmp(method, "SetProperty"))
		return g_variant_ref_sink(g_variant_new("()"));
	if (!strcmp(method, "GetDevices"))
		return g_variant_ref_sink(g_variant_new_parsed("(@ao [],)"));
	g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_SERVICE_UNKNOWN, "fake: no %s", method);
	return NULL;
}
#define g_dbus_connection_emit_signal fake_emit_signal
#define g_dbus_connection_call_sync fake_call_sync
#define main real_main
#include "plinius-satellite.c"
#undef main

static Modem *modem(const char *path)
{
	Modem *m = g_hash_table_lookup(self.modems, path);
	if (!m) {
		m = g_new0(Modem, 1);
		m->path = g_strdup(path);
		m->netreg = m->connman = TRUE;
		g_hash_table_replace(self.modems, m->path, m);
	}
	return m;
}

static void set(Modem *m, const char *iface, const char *key, GVariant *v)
{
	g_variant_ref_sink(v);
	modem_set(m, iface, key, v);
	g_variant_unref(v);
}

int main(int argc, char **argv)
{
	self.modems = g_hash_table_new_full(g_str_hash, g_str_equal, NULL, modem_free);
	self.modem = g_strdup("/");
	load_providers();
	load_policy();
	for (int i = 1; i < argc; i++) {
		g_auto(GStrv) a = g_strsplit(argv[i], "|", -1);
		if (!strcmp(a[0], "match")) {
			const Provider *p = match_provider(a[1], a[2]);
			printf("match %s %s=%s\n", a[1], a[2], p ? p->id : "(none)");
		} else if (!strcmp(a[0], "policy")) {
			printf("policy=%d %d %u\n", self.policy_metered, self.policy_roaming, self.hysteresis_s);
		} else if (!strcmp(a[0], "assess")) {
			/* assess|plmn|name|home|current */
			g_autoptr(GVariant) v = g_variant_ref_sink(assess(a[1], a[2], NULL, *a[3] ? a[3] : NULL,
								      *a[4] ? a[4] : NULL));
			const char *kind, *verdict, *provider;
			g_variant_lookup(v, "Kind", "&s", &kind);
			g_variant_lookup(v, "Verdict", "&s", &verdict);
			g_variant_lookup(v, "Provider", "&s", &provider);
			printf("assess %s=%s %s %s\n", a[1], kind, verdict, provider);
		} else if (!strcmp(a[0], "modem")) {
			/* modem|path|status|mcc|mnc|name */
			Modem *m = modem(a[1]);
			const char *iface = "org.ofono.NetworkRegistration";
			set(m, iface, "Status", g_variant_new_string(a[2]));
			set(m, iface, "MobileCountryCode", g_variant_new_string(a[3]));
			set(m, iface, "MobileNetworkCode", g_variant_new_string(a[4]));
			set(m, iface, "Name", g_variant_new_string(a[5]));
		} else if (!strcmp(a[0], "user-roaming")) {
			set(modem(a[1]), "org.ofono.ConnectionManager", "RoamingAllowed",
			    g_variant_new_boolean(atoi(a[2])));
			printf("held=%s\n", self.roaming_held ? self.roaming_held : "(none)");
		} else if (!strcmp(a[0], "evaluate")) {
			evaluate();
			printf("state: active=%d modem=%s plmn=%s provider=%s held=%s leaving=%d\n", self.active,
			       self.modem, self.plmn ? self.plmn : "", self.provider ? self.provider->id : "",
			       self.roaming_held ? self.roaming_held : "(none)", self.leave_id != 0);
		} else if (!strcmp(a[0], "leave")) {
			on_leave(NULL);
			printf("left: active=%d held=%s\n", self.active, self.roaming_held ? self.roaming_held : "(none)");
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("satellite")
    sandboxed_source(SRC, d, [
        ("/usr/share/plinius-satellite/providers.conf", d / "providers.conf"),
        ("/etc/plinius-satellite/providers.d", d / "providers.d"),
        ("/etc/plinius-satellite/satellite.conf", d / "satellite.conf"),
        ("/var/lib/plinius-satellite/roaming", d / "roaming"),
    ])
    exe = build_harness(d, "satellite", HARNESS, pkgs=("gio-unix-2.0",))
    return d, functools.partial(run, exe)


@pytest.fixture
def sat(built):
    d, binary = built
    shutil.copy(PROVIDERS, d / "providers.conf")
    shutil.rmtree(d / "providers.d", ignore_errors=True)
    (d / "providers.d").mkdir()
    (d / "satellite.conf").unlink(missing_ok=True)
    (d / "roaming").unlink(missing_ok=True)
    return d, binary


@pytest.mark.parametrize("plmn,name,provider", [
    ("310830", "", "T-Mobile US"),                 # by PLMN
    ("310830", "Operator", "T-Mobile US"),         # the PLMN wins over the name
    ("", "T-Mobile SpaceX", "T-Mobile US"),
    ("23410", "o2 satellite", "O2 UK"),            # case does not matter
    ("25503", "Kyivstar SpaceX", "Kyivstar"),      # the specific entry before the catch-all
    ("50501", "Telstra SpaceX", "Starlink"),
    ("53001", "One NZ Starlink", "Starlink"),
    ("26201", "Telekom.de", "(none)"),
    ("31026", "T-Mobile", "(none)"),
    ("", "", "(none)"),
])
def test_shipped_providers(sat, plmn, name, provider):
    d, binary = sat
    out = kv(binary(f"match|{plmn}|{name}").stdout)
    assert out[f"match {plmn} {name}".strip()] == provider


def test_local_overrides(sat):
    d, binary = sat
    (d / "providers.d/20-local.conf").write_text(
        "[Starlink]\nDisabled=true\n\n[Test]\nPlmn=99999\n\n[Empty]\nName=Nothing to match\n")
    (d / "providers.d/10-first.conf").write_text("[Test]\nPlmn=88888\n")
    (d / "providers.d/README").write_text("[Test]\nPlmn=77777\n")     # not a .conf
    out = binary("match|50501|Telstra SpaceX", "match|99999|", "match|88888|", "match|77777|",
                 "match||Nothing to match").stdout
    k = kv(out)
    assert k["match 50501 Telstra SpaceX"] == "(none)"     # switched off locally
    assert k["match 99999"] == "Test"                       # 20-local after 10-first replaces it
    assert k["match 88888"] == "(none)"
    assert k["match 77777"] == "(none)"
    assert k["match  Nothing to match"] == "(none)"


@pytest.mark.parametrize("plmn,name,home,current,expected", [
    ("26201", "Telekom.de", "26201", "", "home yes "),
    ("26201", "Telekom.de", "26201", "26201", "home registered "),
    ("26203", "o2 - de", "26201", "26201", "national unlikely "),
    ("23410", "O2 UK", "26201", "", "roaming maybe "),
    ("23410", "O2 Satellite", "26201", "", "satellite maybe O2 Satellite"),
    ("26203", "", "", "", "roaming maybe "),               # SIM country unknown
])
def test_assess(sat, plmn, name, home, current, expected):
    d, binary = sat
    out = kv(binary(f"assess|{plmn}|{name}|{home}|{current}").stdout)
    assert out[f"assess {plmn}"] == expected.strip()


def test_policy_defaults_and_clamp(sat):
    d, binary = sat
    assert kv(binary("policy").stdout)["policy"] == "1 1 60"
    (d / "satellite.conf").write_text("[Policy]\nMetered=false\nDataRoaming=false\nHysteresis=9999\n")
    assert kv(binary("policy").stdout)["policy"] == "0 0 600"


def states(out):
    return [line[7:] for line in out.splitlines() if line.startswith("state: ")]


def test_on_and_off_the_satellite_with_roaming_data(sat):
    d, binary = sat
    out = binary(
        "modem|/ril_0|roaming|310|830|T-Mobile SpaceX", "evaluate",
        # Back on a tower of the home network: off at once, roaming data off again
        "modem|/ril_0|registered|262|01|Telekom.de", "evaluate").stdout
    s = states(out)
    assert s[0] == "active=1 modem=/ril_0 plmn=310830 provider=T-Mobile US held=/ril_0 leaving=0"
    assert s[1] == "active=0 modem=/ plmn= provider= held=(none) leaving=0"
    calls = [line for line in out.splitlines() if "SetProperty" in line]
    assert calls == [
        "call=/ril_0 org.ofono.ConnectionManager SetProperty('RoamingAllowed', <true>)",
        "call=/ril_0 org.ofono.ConnectionManager SetProperty('RoamingAllowed', <false>)",
    ]
    # The state file is written before roaming goes on and removed after it is off
    assert not (d / "roaming").exists()
    assert "'Active': <true>" in out and "'Provider': <'T-Mobile T-Satellite'>" in out


def test_roaming_state_file_while_on_the_satellite(sat):
    d, binary = sat
    binary("modem|/ril_0|roaming|310|830|", "evaluate")
    # The process ends while on the satellite: the file tells the next run to undo it
    assert (d / "roaming").read_text() == "/ril_0"


def test_no_roaming_data_for_sms_only_provider(sat):
    d, binary = sat
    out = binary("modem|/ril_0|roaming|50501|01|Telstra SpaceX", "evaluate").stdout
    assert states(out)[0].startswith("active=1 ") and "held=(none)" in states(out)[0]
    assert "SetProperty" not in out
    assert not (d / "roaming").exists()


def test_policy_without_data_roaming(sat):
    d, binary = sat
    (d / "satellite.conf").write_text("[Policy]\nDataRoaming=false\n")
    out = binary("modem|/ril_0|roaming|310|830|", "evaluate").stdout
    assert "SetProperty" not in out


def test_lost_satellite_waits_for_hysteresis(sat):
    d, binary = sat
    out = binary("modem|/ril_0|roaming|310|830|", "evaluate",
                 "modem|/ril_0|searching|||", "evaluate", "leave").stdout
    s = states(out)
    # Registration lost: still on the satellite until the hysteresis runs out
    assert s[1].startswith("active=1 ") and s[1].endswith("leaving=1")
    assert "left: active=0 held=(none)" in out


def test_no_hysteresis(sat):
    d, binary = sat
    (d / "satellite.conf").write_text("[Policy]\nHysteresis=0\n")
    out = binary("modem|/ril_0|roaming|310|830|", "evaluate", "modem|/ril_0|searching|||",
                 "evaluate").stdout
    assert states(out)[1].startswith("active=0 ")


def test_user_switching_roaming_off_meanwhile(sat):
    d, binary = sat
    out = binary("modem|/ril_0|roaming|310|830|", "evaluate", "user-roaming|/ril_0|0").stdout
    # Nothing left to undo: no SetProperty false later, state file gone
    assert kv(out)["held"] == "(none)"
    assert not (d / "roaming").exists()


def test_two_modems_first_path_wins(sat):
    d, binary = sat
    out = binary("modem|/ril_1|roaming|310|830|", "modem|/ril_0|roaming|50501|01|Telstra SpaceX",
                 "evaluate").stdout
    assert "modem=/ril_0 " in states(out)[0]
